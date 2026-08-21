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
    """Assert *proc* is evidence of a pass that actually completed.

    Raises ``AssertionError`` naming *what* when it is not.

    WHY the returncode is checked AT ALL, and why FIRST. The live-DC tier used to assert pass
    health with a bare stderr scan — ``assert "Traceback" not in cp.stderr`` — and never looked
    at ``cp.returncode``. That is a fail-OPEN on the epic's exit-evidence path: a child killed
    by a signal is torn down by the kernel before it can write anything, so its stderr is the
    EMPTY STRING, ``"Traceback" not in ""`` is True, and a reaped or OOM-killed reconciler pass
    is recorded as a HEALTHY one. Proven deterministically: a child that SIGKILLs itself yields
    ``returncode == -9`` and ``stderr == ''`` and sails through the old assertion. Same construct
    class as closed bug ``f0fb-de7a-b315-4508``, which routed the ``tests/e2e`` sites through
    :func:`child_failure_detail` in this module. The returncode is therefore checked BEFORE the
    traceback: it is the hole being closed, and a killed child has no stderr left to quote.

    The unacceptable-exit message REUSES :func:`child_failure_detail` (above) rather than
    re-deriving one, so the signal name and returncode a killed child needs (``_exit_detail``,
    ``tests/_child_diag.py:17``) are spelled the same way everywhere. The traceback branch does
    NOT use it: that function's lead sentence is written for a FAILED exit and would read as
    nonsense after an accepted one ("child exited 0 without writing anything to stdout"), so
    that branch quotes the stderr tail directly.

    *ok_codes* defaults to ``(0,)`` because most callers demand a clean exit — e.g. the
    ``bootstrap-strict`` mutation passes at ``tests/external/live_jira_dc/_dc_support.py:658``.
    Callers whose child may legitimately exit non-zero widen it explicitly: the reconciler maps
    the legacy RESCHEDULE disposition to exit 75
    (``src/rebar/_engine/rebar_reconciler/__main__.py:65-72``), which is why
    ``tests/external/live_jira_dc/test_reconcile_pass.py:200`` accepts ``(0, 75)``.
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
    """Assert *proc* ran to completion on its own rather than being killed by a signal.

    The NARROW sibling of :func:`assert_child_ran_clean`, for oracles whose verdict rests on
    a string being ABSENT from a child's output. Such a test fails OPEN: a child killed by a
    signal is torn down by the kernel before it writes anything, so ``stdout == stderr == ""``,
    the bad string is trivially absent, and a run that NEVER EXECUTED is reported GREEN
    (deterministically reproduced: a child running ``kill -9 $$`` yields ``returncode == -9``
    with empty output). Same construct class as bugs ``0e1d-c698-c38d-4c3e`` and
    ``f0fb-de7a-b315-4508``.

    Only a NEGATIVE ``returncode`` is rejected — CPython's encoding of "terminated by signal
    N". This RESTORES the oracle without WIDENING it: callers keep whatever exit-code
    expectation they already had (``!= 0``, ``== 0``, or none at all), and nothing new is
    claimed about the product. In particular a POSITIVE ``128+N`` (e.g. ``137``) is ACCEPTED:
    a shell that survives and reports its own killed child exits normally with that code,
    which is indistinguishable from a script deliberately choosing to exit ``137``.

    :func:`assert_child_ran_clean` is the wrong tool for these sites — it demands a specific
    exit code (default ``0``) and also scans stderr for a traceback, both of which would be
    new assertions about the child. The message is delegated to :func:`child_failure_detail`
    so the signal name and returncode are worded identically everywhere.
    """
    if proc.returncode < 0:
        raise AssertionError(f"{what} did not complete: {child_failure_detail(proc)}")
