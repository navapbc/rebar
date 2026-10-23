"""Human-readable detail for a test-harness child process that failed.

Lives beside the other shared test helpers (``_isolation``, ``_subprocess_env``)
so both the e2e harness and its self-test exercise the same code.
"""

from __future__ import annotations

import signal
import subprocess

#: How much of a child's stderr to keep. Enough for a stack trace's tail
#: without pasting a whole build log into the failure.
STDERR_TAIL_CHARS = 1500


def _exit_detail(returncode: int) -> str:
    """One sentence explaining *returncode*, without reading stderr."""
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:  # pragma: no cover - platform-specific numbers
            name = f"signal {-returncode}"
        return (
            f"child was killed by {name} (returncode {returncode}) — some "
            "external process killed it rather than it failing on its own; a "
            "detached run sharing this checkout may have been reaped"
        )
    if returncode != 0:
        return f"child exited with code {returncode}"
    return "child exited 0 without writing anything to stdout"


def child_failure_detail(proc: subprocess.CompletedProcess[str]) -> str:
    """Describe why *proc* failed, in one line plus its stderr."""
    detail = _exit_detail(proc.returncode)
    stderr = proc.stderr or ""
    if not stderr.strip():
        return f"{detail}; stderr was empty"
    tail = stderr[-STDERR_TAIL_CHARS:]
    elided = "" if len(tail) == len(stderr) else "…(stderr truncated)…\n"
    return f"{detail}; stderr:\n{elided}{tail}"


#: How much of a failing pass's stderr an assertion message quotes. Matches the tail the
#: live-DC sites used inline before they were routed through :func:`assert_child_ran_clean`.
PASS_STDERR_TAIL_CHARS = 2000


def assert_child_ran_clean(
    proc: subprocess.CompletedProcess[str],
    *,
    what: str,
    ok_codes: tuple[int, ...] = (0,),
) -> None:
    """Require an accepted child exit before checking stderr for a traceback.

    A signal-killed child can have empty stderr, so checking only for a traceback would pass
    without execution. Use child_failure_detail for rejected exits and quote stderr directly
    for a traceback after an accepted exit. Most callers accept only exit 0. Reconciler
    RESCHEDULE callers explicitly also accept exit 75.
    """
    if proc.returncode not in ok_codes:
        raise AssertionError(f"{what} did not complete: {child_failure_detail(proc)}")
    stderr = proc.stderr or ""
    if "Traceback" in stderr:
        raise AssertionError(f"{what} raised:\n{stderr[-PASS_STDERR_TAIL_CHARS:]}")


def assert_child_was_not_signal_killed(
    proc: subprocess.CompletedProcess[str],
    *,
    what: str,
) -> None:
    """Reject signal termination without changing the caller's exit-code oracle.

    Negative return codes mean direct signal termination in subprocess. Positive 128+N codes
    remain accepted here because a surviving shell may report a killed child that way. Callers
    retain their own exit-code and output assertions. Child_failure_detail supplies diagnostics.
    """
    if proc.returncode < 0:
        raise AssertionError(f"{what} did not complete: {child_failure_detail(proc)}")
