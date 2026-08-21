"""Process helpers shared between rebar's spawn sites (stdlib-only leaf).

Two families of logic live here because each was once duplicated per caller:

* :func:`reap_process_group` — shared by the grounding harness
  (:mod:`rebar.grounding.harness`) and the reconciler's ACLI transport
  (``rebar_reconciler.adapters.jira.acli_subprocess``): both spawn children with
  ``start_new_session=True`` and, on a wall-clock timeout, must reap the whole process
  GROUP — SIGTERM → grace → SIGKILL → bounded drain — so a pipe-holding grandchild is
  reaped rather than orphaned (bug d843). The logic was duplicated byte-for-byte in the
  two callers (differing only in the grace/drain CONSTANTS and the log identity); this is
  the single source of truth. To keep each caller's timing and log identity,
  it is parameterized by ``grace``/``drain`` timeouts and a ``label``/``logger`` pair.

* :func:`spawn_detached` (+ :func:`detached_child_cwd`) — the ONE implementation of
  "spawn a detached rebar child", shared by the async tickets-branch push
  (``_store.push``), the enrichment drain (``llm.enrich_drain``), the compaction
  sweep (``_commands.compact_trigger``) and the snapshot-GC trigger
  (``_snapshot.gc_trigger``). The pattern was previously copied per site,
  and a defect (the missing durable ``cwd``, bug 3198-438c-72a5-470f) propagated
  by exactly that imitation (task 2dc4-9bcd-75b9-4544).

This module is a **leaf**: stdlib-only (``os`` / ``signal`` / ``subprocess`` /
``logging`` / ``sys``), with NO ``rebar.*`` imports, so both the in-process library and
the path-loaded reconciler subprocess can import it without forming an import cycle.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from typing import IO


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


def detached_child_cwd(tracker: str) -> str:
    """Pick a working directory a detached child can still resolve after its parent's is gone.

    A detached child (the enrichment drain, the compaction sweep, the async
    tickets-branch push) is spawned WITHOUT ``cwd=``, so it inherits the spawning
    command's working directory. This project's own workflow runs ordinary writes from
    short-lived ``make worktree`` worktrees that are removed once the change lands — and
    when the worktree goes, the still-running child's inherited cwd no longer exists, so
    its first ``os.getcwd()`` (reached via ``_config_sources.repo_root``'s ``Path.cwd()``
    fallback) raises ``FileNotFoundError`` and the child dies before doing any work. That
    silently breaks the "outlives the current command" contract each spawn site documents
    (bug 3198-438c-72a5-470f). Passing the result of this helper as ``cwd=`` anchors the
    child to a directory that has nothing to do with whoever spawned it.

    The anchor is the CANONICAL store's repo root: the tracker resolved through symlinks,
    then its PARENT. ``realpath`` is load-bearing rather than cosmetic — a provisioned
    worktree's ``.tickets-tracker`` is a SYMLINK into the main checkout, so without it the
    "root" would be the doomed worktree we are trying to escape. Taking the parent (never
    the tracker itself) matters too: a child sitting inside ``.tickets-tracker`` would
    resolve the repo root to the tracker and then hunt for a tracker inside the tracker.

    Finally, the anchor must EXIST — pointing a child at a missing directory reproduces the
    very failure — so we walk up to the nearest existing ancestor, ultimately the
    filesystem root, which cannot be removed. Total by construction: never raises, because
    every caller detaches under a never-raise posture where a background concern must not
    fail the write or close that triggered it. Even a relative ``tracker`` resolved from an
    already-dead cwd (``realpath`` -> ``getcwd`` -> ``OSError``) degrades to the root.
    """
    try:
        root = os.path.dirname(os.path.realpath(tracker))
    except OSError:
        return os.sep
    while root and not os.path.isdir(root):
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    return root if root and os.path.isdir(root) else os.sep


def _detach_kwargs() -> dict:
    """Platform detach kwargs. POSIX: a new session so the child outlives the parent. Windows
    (authored, API-derisked; NOT reached in v1 — every caller no-ops on nt before spawning):
    DETACHED_PROCESS | CREATE_NO_WINDOW (constants exist only on Windows, referenced only
    inside this branch)."""
    if os.name == "nt":  # pragma: no cover - POSIX CI
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        }
    return {"start_new_session": True, "close_fds": True}


def spawn_detached(
    module: str,
    func: str,
    *args: str,
    env: dict[str, str],
    stderr: int | IO[str],
) -> None:
    """Spawn ``<module>.<func>(*args)`` in a detached bare-python child that outlives this
    process. The single implementation of "spawn a detached rebar child" (task
    2dc4-9bcd-75b9-4544): the PYTHONPATH bootstrap, the ``-c`` re-entry stub, the platform
    detach flags, the stdio discipline and the durable ``cwd`` anchor live HERE, once, so
    the next property this pattern needs cannot silently miss a copy-pasted site the way
    the missing ``cwd=`` did (bug 3198-438c-72a5-470f).

    What stays PER-CALLER, deliberately:

    * ``env`` — construction differs materially between sites (the async push passes a
      secret-stripped ``project_child_env`` projection; the drain/sweep pass a plain
      ``{**os.environ}``), so the caller supplies the finished dict and this helper only
      layers the PYTHONPATH bootstrap onto a COPY of it (the caller's dict is not mutated).
    * ``stderr`` — each caller picks its own sink (a store-scoped log file, or DEVNULL).
    * the failure posture — this helper RE-RAISES whatever ``Popen`` raises, because the
      sites do not catch identically (the push catches only ``OSError``; the drain and the
      sweep catch broad ``Exception``) and folding the catch in here would silently change
      an exposure. Each caller keeps its existing ``except`` clause and log line.

    ``args`` (at least one) become the child's ``sys.argv[1:]``, and the FIRST should be a
    CANONICAL store path — the child outlives ephemeral worktrees, so a worktree symlink
    would die with its worktree (bugs 93a9-66cf-e681-4f49, da68-fc7c-068c-4c53). The child's
    ``cwd`` is anchored via :func:`detached_child_cwd` on that first argument. Arguments are
    plain STRINGS on the child's argv; a caller whose entry point wants an optional value
    passes a sentinel (e.g. ``""``) and coerces it back inside the entry point — the stub
    stays generic. The child is spawned as a bare ``sys.executable`` whose ``rebar``
    importability comes from putting this checkout's ``src`` dir (``parents[1]`` of this
    file) on PYTHONPATH and having the ``-c`` stub re-insert it.
    """
    if not args:
        raise ValueError("spawn_detached needs at least one argument for the child")
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child_env = dict(env)
    child_env["PYTHONPATH"] = src + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    n = len(args)
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, sys.argv[{n + 1}]); "
            f"import {module}; {module}.{func}(*sys.argv[1:{n + 1}])",
            *args,
            src,
        ],
        cwd=detached_child_cwd(args[0]),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        **_detach_kwargs(),
    )
