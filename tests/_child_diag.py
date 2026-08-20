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
