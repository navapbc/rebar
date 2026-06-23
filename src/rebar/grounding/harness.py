"""The fail-open recover/timeout harness every grounding backend runs inside
(epic 8f6c / story 0b2b).

The IRONCLAD invariant of the oracle: a crash / hang / timeout / missing tool /
version-skew becomes a recorded ``abstain``, NEVER a raise and never a false
accusation. This module owns the execution boundary; it does NOT know about jobs
or tiers (a backend concern). Two boundaries:

* **Out-of-process tools** (ctags, ast-grep, OpenGrep, scc/lizard, registry HTTP)
  — :func:`run_tool`. Mirrors the reconciler's ``acli_subprocess`` reaper: spawn
  in its own session so a hung child (or a pipe-holding grandchild) is reaped via
  ``killpg`` (SIGTERM → grace → SIGKILL → bounded drain), not orphaned. A missing
  binary, a timeout, or an OS error becomes a structured fail-open result.
* **In-process bindings** (tree-sitter and other C-extensions) — :func:`run_in_worker`.
  A thread/signal timeout CANNOT interrupt a hung C-extension call, and a segfault
  would kill the host — so the binding runs in a WORKER SUBPROCESS bounded the same
  way. A hang is reaped; a signal death (e.g. SIGSEGV) is caught as a fail-open
  result rather than taking down the host.

The result is a small :class:`RunResult` carrying ``abstain_reason`` (one of the
closed :data:`rebar.grounding.evidence.ABSTAIN_REASONS`) iff a fail-open condition
tripped; the backend turns that into a full ``abstain`` record (it owns job/tier).
stdlib-only; import-clean.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import evidence as ev

logger = logging.getLogger("rebar.grounding")

# Reaper timing — mirror acli_subprocess (SIGTERM grace, then bounded SIGKILL drain).
_GRACE_SECONDS = 3
_DRAIN_SECONDS = 2

#: Default per-invocation timeout (seconds), env-tunable via REBAR_GROUNDING_TIMEOUT.
_DEFAULT_TIMEOUT = 60
_TIMEOUT_ENV = "REBAR_GROUNDING_TIMEOUT"


def _resolve_timeout(timeout: float | None) -> float:
    if timeout is not None:
        return timeout
    raw = os.environ.get(_TIMEOUT_ENV)
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT


@dataclass
class RunResult:
    """The outcome of one fail-open invocation.

    ``abstain_reason`` is set (to a closed :data:`ABSTAIN_REASONS` value) iff a
    fail-open condition tripped — the backend should then emit an ``abstain``.
    Otherwise the process ran (``completed=True``); ``returncode`` may still be
    non-zero (a backend-specific concern, NOT a harness abstain) and ``value``
    carries an in-process worker's return value.
    """

    backend: str
    completed: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    value: Any = None
    abstain_reason: str | None = None
    detail: str | None = None
    version: str | None = None

    @property
    def abstained(self) -> bool:
        return self.abstain_reason is not None

    def as_abstain(self, *, job: str, provenance_tier: str, **extra: Any) -> dict[str, Any]:
        """Convenience: turn a fail-open result into a full ``abstain`` record.

        The backend supplies ``job``/``provenance_tier`` (which the harness does
        not own); ``extra`` is forwarded to :func:`evidence.abstain`.
        """
        if not self.abstained:
            raise ValueError("as_abstain() called on a non-abstained RunResult")
        return ev.abstain(
            self.abstain_reason,  # type: ignore[arg-type]
            job=job,
            provenance_tier=provenance_tier,
            backend=self.backend,
            version=self.version,
            detail=self.detail,
            **extra,
        )


# ── Out-of-process boundary ──────────────────────────────────────────────────


def _reap_process_group(p: subprocess.Popen[str]) -> None:
    """Terminate and reap a timed-out child and its whole process group.

    Mirrors ``acli_subprocess._reap_process_group`` (bug d843): on POSIX the child
    leads its own session (``start_new_session=True``), so we ``killpg`` the group
    (SIGTERM, grace, then SIGKILL) to catch pipe-holding grandchildren a direct
    ``p.kill()`` would orphan. Every ``getpgid``/``killpg`` is guarded against the
    ESRCH/EPERM race; the post-kill drain is bounded so a D-state child can't hang
    us forever (a survivor is logged as leaked, never asserted).
    """
    if os.name != "posix":
        try:
            p.kill()
        except ProcessLookupError:
            pass
        try:
            p.wait(timeout=_GRACE_SECONDS + _DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("grounding child PID %s did not exit after kill (leaked)", p.pid)
        return

    try:
        pgid = os.getpgid(p.pid)
    except (ProcessLookupError, PermissionError):
        try:
            p.wait(timeout=_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.communicate(timeout=_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.communicate(timeout=_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning(
            "grounding process group %s survived SIGKILL after %ss drain (leaked PID %s)",
            pgid,
            _DRAIN_SECONDS,
            p.pid,
        )


def run_tool(
    cmd: Sequence[str],
    *,
    backend: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    version: str | None = None,
    expected_version: str | None = None,
) -> RunResult:
    """Run an out-of-process backend tool inside the fail-open boundary.

    Returns a :class:`RunResult`. Fail-open mappings (each → an ``abstain_reason``,
    never a raise):

    * binary not found / not executable → ``no_tool``
    * the call exceeds ``timeout`` → the group is reaped → ``timeout``
    * ``expected_version`` given and ``version`` differs → ``version_skew`` (the
      tool is NOT run — a skewed backend's output is untrustworthy)
    * any other OSError spawning the process → ``other``

    A process that runs to completion returns ``completed=True`` with the captured
    stdout/stderr/returncode for the backend to parse (a non-zero exit is the
    backend's call, not a harness abstain).
    """
    if expected_version is not None and version is not None and expected_version != version:
        return RunResult(
            backend=backend,
            completed=False,
            abstain_reason="version_skew",
            version=version,
            detail=f"{backend} version {version!r} != pinned {expected_version!r}",
        )

    call_timeout = _resolve_timeout(timeout)
    popen_kwargs: dict[str, Any] = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",  # a SIGKILL mid-multibyte must not crash the reap
        env=env,
    )
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # POSIX-only (killpg needs it)
    try:
        p = subprocess.Popen(list(cmd), **popen_kwargs)
    except (FileNotFoundError, NotADirectoryError):
        return RunResult(
            backend=backend,
            completed=False,
            abstain_reason="no_tool",
            version=version,
            detail=f"{backend} binary not found: {cmd[0]!r}",
        )
    except PermissionError:
        return RunResult(
            backend=backend,
            completed=False,
            abstain_reason="no_tool",
            version=version,
            detail=f"{backend} binary not executable: {cmd[0]!r}",
        )
    except OSError as exc:
        return RunResult(
            backend=backend,
            completed=False,
            abstain_reason="other",
            version=version,
            detail=f"{backend} spawn failed: {exc}",
        )

    try:
        out, err = p.communicate(timeout=call_timeout)
    except subprocess.TimeoutExpired:
        _reap_process_group(p)
        return RunResult(
            backend=backend,
            completed=False,
            abstain_reason="timeout",
            version=version,
            detail=f"{backend} exceeded {call_timeout}s",
        )
    except OSError as exc:
        # A broken-pipe / read error mid-communicate must fail open, not propagate.
        _reap_process_group(p)
        return RunResult(
            backend=backend,
            completed=False,
            abstain_reason="other",
            version=version,
            detail=f"{backend} communicate failed: {exc}",
        )
    return RunResult(
        backend=backend,
        completed=True,
        returncode=p.returncode,
        stdout=out,
        stderr=err,
        version=version,
    )


# ── In-process boundary (worker subprocess) ──────────────────────────────────


def _worker_entry(conn: Any, func: Callable[..., Any], args: tuple, kwargs: dict) -> None:
    """Worker-subprocess trampoline: run ``func`` and ship its result back.

    A raise is shipped as ``("err", repr)``; a hang or a hard crash (segfault) is
    NOT shippable — the parent detects those via join-timeout / a negative
    exitcode (signal death) and maps them to a fail-open abstain.
    """
    try:
        result = func(*args, **(kwargs or {}))
        conn.send(("ok", result))
    except BaseException as exc:  # noqa: BLE0001 — fail-open: any error becomes evidence, never a host crash
        conn.send(("err", f"{type(exc).__name__}: {exc}"))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _worker_context() -> Any:
    """Pick a multiprocessing context.

    On POSIX prefer ``fork``: the worker inherits the (already-imported) bindings
    with no pickling and no module re-import, which is both faster and avoids the
    spawn re-import hazard for callables defined in test/consumer modules. Elsewhere
    fall back to the platform default (``spawn`` on Windows).
    """
    if os.name == "posix":
        try:
            return multiprocessing.get_context("fork")
        except ValueError:
            pass
    return multiprocessing.get_context()


def run_in_worker(
    func: Callable[..., Any],
    *args: Any,
    backend: str,
    timeout: float | None = None,
    version: str | None = None,
    expected_version: str | None = None,
    kwargs: dict[str, Any] | None = None,
) -> RunResult:
    """Run an in-process binding ``func`` inside a worker subprocess, fail-open.

    The same reaper discipline as :func:`run_tool`, extended to the one thing a
    thread/signal timeout can't survive — a C-extension that hangs or segfaults.
    Fail-open mappings (each → an ``abstain_reason``, NEVER a raise):

    * ``expected_version`` given and ``version`` differs → ``version_skew`` (the
      binding is NOT run — a skewed ABI's output is untrustworthy)
    * the worker exceeds ``timeout`` → the process is terminated/killed → ``timeout``
    * the worker dies on a signal (e.g. SIGSEGV/SIGABRT from a bad C parse, or an
      OOM kill) → ``parse_error`` (recorded, never a host crash)
    * the worker raises → ``other`` (with the exception repr in ``detail``)
    * spawning the worker fails (fork/pipe ``OSError`` under FD/process pressure) →
      ``other`` (the fail-open invariant must hold even when the host is exhausted)
    * the worker returns cleanly → ``completed=True`` with ``value`` set

    The result pipe is drained CONCURRENTLY with the wait (via ``poll`` then
    ``recv``), so a worker returning a payload larger than the OS pipe buffer
    (~64 KB) does NOT deadlock-then-spuriously-time-out — the parent reads as the
    child writes.
    """
    if expected_version is not None and version is not None and expected_version != version:
        return RunResult(
            backend=backend,
            completed=False,
            abstain_reason="version_skew",
            version=version,
            detail=f"{backend} version {version!r} != pinned {expected_version!r}",
        )

    call_timeout = _resolve_timeout(timeout)
    ctx = _worker_context()
    parent_conn = child_conn = None
    proc = None
    try:
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_worker_entry, args=(child_conn, func, args, kwargs or {}))
        proc.start()
    except OSError as exc:
        # Fork/pipe failure under resource pressure (EMFILE/EAGAIN/ENOMEM) must
        # fail open, not propagate — this harness wraps every in-process backend.
        _safe_close(parent_conn, child_conn)
        _safe_close_proc(proc)
        return RunResult(
            backend=backend,
            completed=False,
            abstain_reason="other",
            version=version,
            detail=f"{backend} worker spawn failed: {exc}",
        )
    child_conn.close()  # parent keeps only the read end

    try:
        payload = None
        try:
            # poll() returns as soon as the child sends (or closes the pipe on
            # exit/crash), so a large result drains here instead of deadlocking a
            # join-before-read; recv() then reads the whole framed message.
            if parent_conn.poll(call_timeout):
                payload = parent_conn.recv()
        except EOFError:
            payload = None  # child closed the pipe without sending (exit/crash)

        if payload is None and proc.is_alive():
            # Nothing arrived within the timeout and the worker is still running → hang.
            _reap_worker(proc)
            return RunResult(
                backend=backend,
                completed=False,
                abstain_reason="timeout",
                version=version,
                detail=f"{backend} worker exceeded {call_timeout}s",
            )

        # The worker produced a result or died; ensure it is reaped before inspecting.
        proc.join(_DRAIN_SECONDS)
        if proc.is_alive():
            _reap_worker(proc)
        exitcode = proc.exitcode

        if payload is None:
            if exitcode is not None and exitcode < 0:
                signame = _signal_name(-exitcode)
                return RunResult(
                    backend=backend,
                    completed=False,
                    abstain_reason="parse_error",
                    version=version,
                    detail=f"{backend} worker killed by {signame} "
                    "(in-process binding crashed or was killed)",
                )
            return RunResult(
                backend=backend,
                completed=False,
                abstain_reason="other",
                version=version,
                detail=f"{backend} worker exited {exitcode} with no result",
            )
        tag, body = payload
        if tag == "ok":
            return RunResult(
                backend=backend, completed=True, returncode=exitcode, value=body, version=version
            )
        return RunResult(
            backend=backend,
            completed=False,
            abstain_reason="other",
            version=version,
            detail=f"{backend} worker raised: {body}",
        )
    finally:
        _safe_close(parent_conn)
        _safe_close_proc(proc)


def _reap_worker(proc: Any) -> None:
    """SIGTERM → grace → SIGKILL a hung worker process, bounded drain."""
    try:
        proc.terminate()
    except Exception:
        pass
    proc.join(_GRACE_SECONDS)
    if proc.is_alive():
        try:
            proc.kill()
        except Exception:
            pass
        proc.join(_DRAIN_SECONDS)
        if proc.is_alive():
            logger.warning("grounding worker PID %s survived SIGKILL (leaked)", proc.pid)


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except (ValueError, AttributeError):
        return f"signal {signum}"


def _safe_close(*conns: Any) -> None:
    """Close pipe Connection ends, ignoring already-closed / None."""
    for conn in conns:
        if conn is None:
            continue
        try:
            conn.close()
        except OSError:
            pass


def _safe_close_proc(proc: Any) -> None:
    """Release a Process's sentinel FD (``close()``), once it is no longer alive.

    A live process raises ``ValueError`` on ``close()``; that path keeps the FD
    until GC (acceptable for a process we failed to reap) rather than raising.
    """
    if proc is None:
        return
    try:
        proc.close()
    except (ValueError, OSError):
        pass
