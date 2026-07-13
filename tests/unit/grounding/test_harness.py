"""Unit tests for rebar.grounding.harness — the fail-open execution boundary.

The IRONCLAD invariant: every failure mode becomes a recorded fail-open result
(an abstain_reason from the closed set), NEVER a raise. Covers BOTH boundaries:

* out-of-process (run_tool): missing tool, timeout (reaped), version-skew, success
* in-process (run_in_worker): clean return, a raise, a hang (reaped), and a hard
  crash / signal death (a stand-in for a segfaulting C-extension parse)
"""

from __future__ import annotations

import sys

import pytest

from rebar.grounding import evidence as ev
from rebar.grounding import harness

from . import _worker_payloads as wp

pytestmark = pytest.mark.unit


# ── out-of-process: run_tool ─────────────────────────────────────────────────


def test_run_tool_success_returns_completed_output() -> None:
    r = harness.run_tool([sys.executable, "-c", "print('ok')"], backend="py")
    assert r.completed and not r.abstained
    assert r.returncode == 0
    assert r.stdout.strip() == "ok"


def test_run_tool_nonzero_exit_is_not_an_abstain() -> None:
    # A non-zero exit is a backend concern, NOT a harness fail-open condition.
    r = harness.run_tool([sys.executable, "-c", "import sys; sys.exit(3)"], backend="py")
    assert r.completed and not r.abstained
    assert r.returncode == 3


def test_missing_binary_maps_to_no_tool() -> None:
    r = harness.run_tool(["this-binary-does-not-exist-xyzzy"], backend="ctags")
    assert r.abstained and r.abstain_reason == "no_tool"
    rec = r.as_abstain(job=ev.JOB_REFUTE, provenance_tier=ev.TIER_T1)
    assert rec["outcome"] == "abstain" and rec["reason"] == "no_tool"
    assert rec["coverage"]["status"] == "skipped"
    ev.validate(rec)


def test_timeout_is_reaped_and_maps_to_timeout() -> None:
    # Spawn a child that sleeps far past the timeout; the group must be reaped and
    # a timeout abstain returned (never a hang, never a raise).
    r = harness.run_tool(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        backend="slowtool",
        timeout=0.5,
    )
    assert r.abstained and r.abstain_reason == "timeout"
    ev.validate(r.as_abstain(job=ev.JOB_SMELL, provenance_tier=ev.TIER_T1))


def test_timeout_reaps_pipe_holding_grandchild() -> None:
    # A child that forks a grandchild holding the stdout pipe: a plain p.kill()
    # would orphan the grandchild and communicate() would still block. The
    # process-group reaper must kill the whole group and return within the drain.
    code = (
        "import os, time, sys\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(60)\n"  # grandchild holds the inherited stdout pipe
        "else:\n"
        "    time.sleep(60)\n"
    )
    r = harness.run_tool([sys.executable, "-c", code], backend="forky", timeout=0.5)
    assert r.abstained and r.abstain_reason == "timeout"


def test_version_skew_short_circuits_without_running() -> None:
    # If the recorded version disagrees with the pinned one, the tool is NOT run.
    r = harness.run_tool(
        [sys.executable, "-c", "raise SystemExit('should not run')"],
        backend="ctags",
        version="6.0.0",
        expected_version="6.2.1",
    )
    assert r.abstained and r.abstain_reason == "version_skew"
    assert r.version == "6.0.0"
    ev.validate(r.as_abstain(job=ev.JOB_REFUTE, provenance_tier=ev.TIER_T1))


# ── in-process: run_in_worker ────────────────────────────────────────────────


def test_worker_clean_return() -> None:
    r = harness.run_in_worker(wp.returns_value, 21, backend="ts")
    assert r.completed and not r.abstained
    assert r.value == 42


def test_worker_kwargs_passthrough() -> None:
    r = harness.run_in_worker(wp.returns_kwarg, backend="ts", kwargs={"name": "rebar"})
    assert r.completed and r.value == "hello rebar"


def test_worker_raise_maps_to_other() -> None:
    r = harness.run_in_worker(wp.raises_error, backend="ts")
    assert r.abstained and r.abstain_reason == "other"
    assert "boom in worker" in (r.detail or "")
    ev.validate(r.as_abstain(job=ev.JOB_SMELL, provenance_tier=ev.TIER_T1))


class _FakeConn:
    """Records what the worker trampoline ships back, in-process (no subprocess)."""

    def __init__(self) -> None:
        self.sent: list = []
        self.closed = False

    def send(self, msg: object) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        self.closed = True


def test_worker_entry_ships_regular_exception_as_evidence() -> None:
    """A regular Exception in the worker is caught and shipped as ('err', ...) — preserved."""

    def boom() -> None:
        raise ValueError("kaboom")

    conn = _FakeConn()
    harness._worker_entry(conn, boom, (), {})
    assert conn.sent and conn.sent[0][0] == "err"
    assert "ValueError: kaboom" in conn.sent[0][1]
    assert conn.closed


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_worker_entry_propagates_interrupts(interrupt: type[BaseException]) -> None:
    """Narrowing regression (epic ring-gun-jot): the worker boundary was narrowed from
    `except BaseException` (an inert `# noqa: BLE0001` typo) to `except Exception`, so a
    KeyboardInterrupt / SystemExit now PROPAGATES (the parent maps signal-death to a
    fail-open abstain) instead of being swallowed and shipped as 'err'. The pipe is still
    closed by the `finally`."""

    def raise_interrupt() -> None:
        raise interrupt()

    conn = _FakeConn()
    with pytest.raises(interrupt):
        harness._worker_entry(conn, raise_interrupt, (), {})
    assert not any(msg[0] == "err" for msg in conn.sent), "interrupt must not ship as evidence"
    assert conn.closed, "the finally must still close the pipe"


def test_worker_hang_is_reaped_and_maps_to_timeout() -> None:
    r = harness.run_in_worker(wp.hangs_forever, backend="ts", timeout=0.5)
    assert r.abstained and r.abstain_reason == "timeout"
    ev.validate(r.as_abstain(job=ev.JOB_SMELL, provenance_tier=ev.TIER_T1))


def test_worker_hard_crash_maps_to_parse_error_not_host_crash() -> None:
    # os.abort() in the worker (SIGABRT) stands in for a segfaulting C-extension
    # parse. The host must survive and record a parse_error abstain.
    r = harness.run_in_worker(wp.hard_crash, backend="tree-sitter")
    assert r.abstained and r.abstain_reason == "parse_error"
    ev.validate(r.as_abstain(job=ev.JOB_SMELL, provenance_tier=ev.TIER_T1))
    # Prove the host is unharmed: a subsequent worker call still works.
    again = harness.run_in_worker(wp.returns_value, 5, backend="ts")
    assert again.completed and again.value == 10


def test_worker_large_result_does_not_deadlock_into_a_false_timeout() -> None:
    # A 1 MB return value far exceeds the ~64 KB pipe buffer. A join-before-read
    # harness deadlocks and reports a spurious timeout; the concurrent drain must
    # return the real value well within the timeout.
    n = 1_000_000
    r = harness.run_in_worker(wp.returns_big, n, backend="ts", timeout=10)
    assert r.completed and not r.abstained, (
        f"large result misreported: {r.abstain_reason} ({r.detail})"
    )
    assert isinstance(r.value, str) and len(r.value) == n


def test_worker_version_skew_short_circuits() -> None:
    # Parity with run_tool: a skewed binding ABI is not run.
    r = harness.run_in_worker(
        wp.returns_value, 1, backend="tree-sitter", version="0.20", expected_version="0.21"
    )
    assert r.abstained and r.abstain_reason == "version_skew"
    ev.validate(r.as_abstain(job=ev.JOB_SMELL, provenance_tier=ev.TIER_T1))


def test_worker_spawn_failure_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fork/pipe exhaustion (OSError on start) must become an abstain, never a raise.
    real_ctx = harness._worker_context()

    class _Boom:
        def Pipe(self, *a, **k):
            return real_ctx.Pipe(*a, **k)

        def Process(self, *a, **k):
            class _P:  # a real multiprocessing.Process has start()/close()
                def start(self_inner):
                    raise OSError("cannot fork: resource temporarily unavailable")

                def close(self_inner):
                    pass

            return _P()

    monkeypatch.setattr(harness, "_worker_context", lambda: _Boom())
    r = harness.run_in_worker(wp.returns_value, 1, backend="ts")
    assert r.abstained and r.abstain_reason == "other"
    assert "spawn failed" in (r.detail or "")
    ev.validate(r.as_abstain(job=ev.JOB_SMELL, provenance_tier=ev.TIER_T1))


def test_as_abstain_on_non_abstained_raises() -> None:
    r = harness.run_tool([sys.executable, "-c", "pass"], backend="py")
    with pytest.raises(ValueError):
        r.as_abstain(job=ev.JOB_REFUTE, provenance_tier=ev.TIER_T1)


def test_signal_death_not_misclassified_as_timeout_under_reap_lag(monkeypatch) -> None:
    """Bug 85c3: a signal-killed worker whose exit hasn't been reaped yet must map to
    parse_error, NOT timeout — even if ``proc.is_alive()`` transiently returns True.

    Deterministically reproduces the crash-vs-timeout race by forcing the proc's
    ``is_alive()`` to report True exactly once (simulating reap lag right after the
    signal death). The classifier must key on whether the poll() timeout actually
    elapsed — a crash closes the pipe immediately (poll → True), so it can never be a
    timeout — not on the racy is_alive() probe.

    RED (pre-fix, is_alive()-gated classifier): returns abstain_reason=="timeout".
    GREEN (poll-gated classifier): returns abstain_reason=="parse_error".
    """
    real_ctx = harness._worker_context()

    class _ReapLagCtx:
        """Wrap the real context so the spawned proc reports is_alive()==True once."""

        def Pipe(self, *args, **kwargs):
            return real_ctx.Pipe(*args, **kwargs)

        def Process(self, *args, **kwargs):
            proc = real_ctx.Process(*args, **kwargs)
            real_is_alive = proc.is_alive
            forced = {"done": False}

            def fake_is_alive():
                # First probe after the crash: pretend the killed child isn't reaped
                # yet (the exact transient the bug misread as a live→timeout worker).
                if not forced["done"]:
                    forced["done"] = True
                    return True
                return real_is_alive()

            proc.is_alive = fake_is_alive  # type: ignore[method-assign]
            return proc

    monkeypatch.setattr(harness, "_worker_context", lambda: _ReapLagCtx())

    r = harness.run_in_worker(wp.hard_crash, backend="tree-sitter")
    assert r.abstained, "a signal-killed worker must fail open, not complete"
    assert r.abstain_reason == "parse_error", (
        f"signal death misclassified as {r.abstain_reason!r} under reap lag "
        "(crash-vs-timeout race, bug 85c3)"
    )
    ev.validate(r.as_abstain(job=ev.JOB_SMELL, provenance_tier=ev.TIER_T1))
