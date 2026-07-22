"""Reap a timed-out child process and its whole process group (stdlib-only leaf).

Shared by the grounding harness (:mod:`rebar.grounding.harness`) and the
reconciler's ACLI transport (``rebar_reconciler.adapters.jira.acli_subprocess``): both spawn
children with ``start_new_session=True`` and, on a wall-clock timeout, must reap
the whole process GROUP — SIGTERM → grace → SIGKILL → bounded drain — so a
pipe-holding grandchild is reaped rather than orphaned (bug d843). The logic was
duplicated byte-for-byte in the two callers (differing only in the grace/drain
CONSTANTS and the log identity); this is the single source of truth. To keep each
caller's timing and log identity, :func:`reap_process_group` is parameterized by
``grace``/``drain`` timeouts and a ``label``/``logger`` pair.

This module is a **leaf**: stdlib-only (``os`` / ``signal`` / ``subprocess`` /
``logging``), with NO ``rebar.*`` imports, so both the in-process library and the
path-loaded reconciler subprocess can import it without forming an import cycle.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess


def reap_process_group(
    proc: subprocess.Popen[str],
    *,
    grace: float,
    drain: float,
    label: str,
    logger: logging.Logger,
) -> None:
    """Terminate and reap a timed-out child and its whole process group (bug d843).

    On POSIX the child was started with ``start_new_session=True`` so it leads its
    own group; we ``killpg`` the group (SIGTERM, ``grace``, then SIGKILL) to catch
    pipe-holding grandchildren that a direct ``proc.kill()`` would orphan (validation
    spikes E1/E2). All ``getpgid`` / ``killpg`` calls are guarded against the
    ESRCH/EPERM race (spike E5: an already-exited group raises ``ProcessLookupError``).
    The post-kill ``drain`` is itself bounded so a D-state (unkillable) child can't
    block forever — a survivor is logged as a leaked PID, never asserted.

    On non-POSIX (no ``killpg``) fall back to ``proc.kill()`` + a bounded wait.

    ``label`` names the caller in leak-warning log lines (e.g. ``"grounding"`` /
    ``"acli"``); ``logger`` is the caller's logger so those warnings keep the
    caller's identity. ``grace``/``drain`` are the caller's own timing constants.
    """
    if os.name != "posix":
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=grace + drain)
        except subprocess.TimeoutExpired:
            logger.warning("%s child PID %s did not exit after kill (leaked)", label, proc.pid)
        return

    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        # Child already gone (ESRCH) or we can't see it — best-effort reap and return.
        try:
            proc.wait(timeout=drain)
        except subprocess.TimeoutExpired:
            pass
        return

    # SIGTERM the group, then give it a grace window to flush + exit cleanly.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=grace)
        return  # exited on SIGTERM within the grace window — drained.
    except subprocess.TimeoutExpired:
        pass

    # Grace expired — SIGKILL the group, then bound the final reap/drain so a
    # D-state child cannot hang us indefinitely.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=drain)
    except subprocess.TimeoutExpired:
        logger.warning(
            "%s process group %s survived SIGKILL after %ss drain (leaked PID %s)",
            label,
            pgid,
            drain,
            proc.pid,
        )
