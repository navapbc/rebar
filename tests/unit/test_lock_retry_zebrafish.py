"""A write whose lock acquire times out must be RETRIED, not permanently lost.

Bug ``royal-weariless-zebrafish``. Before this, ``acquire()`` folded ``timeout × attempts``
into ONE deadline and raised :class:`LockTimeout` when it expired — the write was discarded
with no retry and no spool. Measured against a clone of the live store: a 70s holder plus 3
``rebar comment``s produced 3/3 failures and **0 of 3 markers present afterwards**, and a
``claim`` behind a 45s holder died at exactly 30.30s because it passes ``attempts=1``.

The remedy is bounded retry-with-backoff rather than fair queueing: when one holder outlives
the whole budget, EVERY waiter fails regardless of service order, so FIFO buys nothing against
the failure that actually occurs. Retry is safe — and cannot duplicate a write — precisely
because it lives inside ``acquire()``, before the caller's write body has run.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

from rebar._commands import txn
from rebar._store import lock as _lock

# One pass is deliberately tiny so the suite runs in seconds; the SHAPE under test (a holder
# that outlives a single budget by 2.5x) is identical to the 70s-holder-vs-60s-budget field
# case. Keep the production retry backoff real: only this fixture's pass/holder windows scale.
_PASS_S = 0.2
_HOLD_S = 0.5


def _spawn_holder(tracker: str, hold_s: float) -> subprocess.Popen[str]:
    """Hold the real dual-window lock in a SEPARATE process — the only way to reproduce
    genuine cross-process contention (an in-process fake would prove nothing about flock)."""
    src = (
        "import sys, time\n"
        "from rebar._store import lock\n"
        "h = lock.acquire(sys.argv[1], timeout=30, attempts=1)\n"
        "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
        "time.sleep(float(sys.argv[2]))\n"
        "h.release()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", src, tracker, str(hold_s)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"  # blocking: the lock IS taken
    return proc


# --------------------------------------------------------------- the loss, and the remedy


def test_reproduction_timed_out_write_is_lost_without_retries(tmp_path):
    """RED: this is the bug. One budget, a holder that outlives it, and the write is GONE."""
    proc = _spawn_holder(str(tmp_path), _HOLD_S)
    try:
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(str(tmp_path), timeout=_PASS_S, attempts=1, retries=0)
    finally:
        proc.kill()
        proc.wait()


def test_reproduction_retries_ride_out_the_same_holder(tmp_path):
    """GREEN: the identical holder is ridden out — the loss became latency."""
    proc = _spawn_holder(str(tmp_path), _HOLD_S)
    try:
        handle = _lock.acquire(str(tmp_path), timeout=_PASS_S, attempts=1, retries=3)
        handle.release()
    finally:
        proc.kill()
        proc.wait()


def test_retry_never_replays_the_callers_write_body(tmp_path):
    """The safety property the whole design rests on: retrying an ACQUISITION must never
    re-run the write it guards, or a retried ``comment`` would append twice. Holds because
    the loop sits inside ``acquire()`` — the body has not run when a pass expires."""
    appended: list[str] = []
    proc = _spawn_holder(str(tmp_path), _HOLD_S)
    try:
        with _lock.write_lock(str(tmp_path), timeout=_PASS_S, attempts=1, retries=3):
            appended.append("EVENT")
    finally:
        proc.kill()
        proc.wait()

    assert appended == ["EVENT"], "the guarded write body ran more than once"


# ------------------------------------------------------------------- loud, honest failure


def test_exhausting_every_pass_still_fails_loudly_with_cumulative_wait(tmp_path, caplog):
    """A write that cannot be committed must never be SILENTLY discarded: it fails, and the
    message reports the CUMULATIVE wait rather than one pass's budget (which would understate
    how long the caller actually waited)."""
    proc = _spawn_holder(str(tmp_path), 30)
    try:
        with caplog.at_level("WARNING", logger="rebar._store.lock"):
            with pytest.raises(_lock.LockTimeout) as excinfo:
                _lock.acquire(str(tmp_path), timeout=_PASS_S, attempts=1, retries=2)
    finally:
        proc.kill()
        proc.wait()

    # 3 passes x 1s + backoff — strictly more than the single-pass budget it replaces.
    assert excinfo.value.total_wait >= 3 * _PASS_S
    assert "could not acquire lock" in str(excinfo.value)
    # One warning per EXHAUSTED pass (the final one raises instead of warning), so a starved
    # writer is visible in logs even where a caller swallows the exception.
    retry_warnings = [r for r in caplog.records if "still held after" in r.getMessage()]
    assert len(retry_warnings) == 2
    assert "pass 1/3" in retry_warnings[0].getMessage()


def test_single_budget_message_is_unchanged_when_retries_are_off(tmp_path):
    """``retries=0`` must be today's behaviour bit-for-bit — the historical message shape is
    load-bearing for callers keyed on it (bug 7084's holder suffix, completion_sidecar)."""
    proc = _spawn_holder(str(tmp_path), 10)
    try:
        with pytest.raises(_lock.LockTimeout) as excinfo:
            _lock.acquire(str(tmp_path), timeout=1, attempts=1, retries=0)
    finally:
        proc.kill()
        proc.wait()

    assert str(excinfo.value).startswith("flock: could not acquire lock after 1s")


# ------------------------------------------------------------------------- the claim verb


def test_claim_shaped_acquire_survives_an_over_budget_holder(tmp_path):
    """`claim` is the most contended verb and was measured dying FIRST (30.30s) because it
    passes ``attempts=1``. The same shape must now survive."""
    proc = _spawn_holder(str(tmp_path), _HOLD_S)
    try:
        handle = _lock.acquire(str(tmp_path), timeout=_PASS_S, attempts=1, retries=3)
        handle.release()
    finally:
        proc.kill()
        proc.wait()


def test_txn_opts_in_to_retries(monkeypatch):
    """The claim/transition critical section must actually request retries — otherwise the
    verb that fails first stays unfixed."""
    seen: dict[str, object] = {}

    def fake_acquire(tracker, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(txn.lock, "acquire", fake_acquire)
    txn._acquire_write_lock("/tmp/whatever")

    assert seen["retries"] == _lock.write_path_retries()
    assert seen["retries"] > 0


def test_txn_propagates_the_holder_and_wait_instead_of_a_generic_error(monkeypatch):
    """The honest cumulative-wait message must SURVIVE the CommandError re-raise. Previously
    ``txn`` replaced it with a bare 'Error: could not acquire lock', discarding both the wait
    and the holder — so the verb this bug most wants to make honest stayed mute."""

    def boom(tracker, **kwargs):
        raise _lock.LockTimeout(181, "host=h pid=4321 held=163s pid_state=live")

    monkeypatch.setattr(txn.lock, "acquire", boom)

    with pytest.raises(txn.CommandError) as excinfo:
        txn._acquire_write_lock("/tmp/whatever")

    message = str(excinfo.value)
    assert "181s" in message, "the cumulative wait must reach the caller"
    assert "pid=4321" in message, "the holder must reach the caller"


# ---------------------------------------------------------------------- posture and knobs


def test_retries_default_is_zero_so_unedited_callers_are_unchanged():
    """Every caller NOT opted in keeps fail-fast with no edit. This default is what keeps
    compaction standing aside (7084 R3) instead of becoming the long holder again."""
    import inspect

    assert inspect.signature(_lock.acquire).parameters["retries"].default == 0
    assert inspect.signature(_lock.write_lock).parameters["retries"].default == 0


def test_uncontended_acquire_never_sleeps_a_backoff(tmp_path, monkeypatch):
    """The retry loop must cost nothing when there is no contention."""
    slept: list[float] = []
    monkeypatch.setattr(_lock, "_backoff_sleep", lambda s: slept.append(s))

    handle = _lock.acquire(str(tmp_path), timeout=_PASS_S, attempts=1, retries=3)
    handle.release()

    assert slept == []


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (None, 2),
        ("", 2),
        ("0", 0),  # restores the historical fail-fast for CI/ops
        ("5", 5),
        ("999", 10),  # clamped to the ceiling
        ("-3", 0),  # clamped to the floor
        ("garbage", 2),  # a malformed knob must not break every write
    ],
)
def test_env_override_bounds_the_ceiling(monkeypatch, env, expected):
    monkeypatch.delenv("REBAR_LOCK_RETRIES", raising=False)
    if env is not None:
        monkeypatch.setenv("REBAR_LOCK_RETRIES", env)
    assert _lock.write_path_retries() == expected


def test_env_override_zero_restores_single_budget_failure(tmp_path, monkeypatch):
    """`REBAR_LOCK_RETRIES=0` at an OPTED-IN site must fail on one budget, exactly as before."""
    monkeypatch.setenv("REBAR_LOCK_RETRIES", "0")
    proc = _spawn_holder(str(tmp_path), _HOLD_S)
    try:
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(
                str(tmp_path), timeout=_PASS_S, attempts=1, retries=_lock.write_path_retries()
            )
    finally:
        proc.kill()
        proc.wait()


# ------------------------------------------------------- the opted-in set, by AST not grep


def _opt_in_sites() -> dict[str, int]:
    """Every ``acquire``/``write_lock`` call in src that passes ``retries``, counted per file.

    Parsed from the AST rather than matched by line number: this file's plan was itself
    blocked once for citing drifted line numbers, and a call-site inventory keyed on them
    rots the moment anything above it moves."""
    root = Path(__file__).resolve().parents[2] / "src" / "rebar"
    found: dict[str, int] = {}
    for module in parsed_python_files(root):
        tree = module.tree
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if name not in {"acquire", "write_lock"}:
                continue
            if any(kw.arg == "retries" for kw in node.keywords):
                found[module.path.name] = found.get(module.path.name, 0) + 1
    return found


def test_opted_in_sites_are_exactly_the_declared_set():
    """The blast radius is the whole point: compaction, fsck, doctor, the reconciler and the
    best-effort sweeps must NOT gain retries. Compaction especially — retrying there would
    undo 7084's stand-aside and re-create the 48s holder that caused this bug."""
    assert _opt_in_sites() == {"event_append.py": 3, "txn.py": 1, "push.py": 1}


def test_advisory_push_merge_stays_fail_fast():
    """`push.py` has TWO lock sites: the genuine commit-and-push opts in, the advisory merge
    stays fail-fast so a contended merge never delays an unrelated command."""
    assert _opt_in_sites()["push.py"] == 1
