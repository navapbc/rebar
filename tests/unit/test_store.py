"""Unit tests for rebar._store — the Tier D write/sync core contracts.

Pins the byte/exit-code/parse surfaces directly (the bash suites cover the
end-to-end paths): canonical committed bytes, the 75 rebase guard, lock timeout,
the event_type enum, and REBAR_PUSH parsing.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from rebar._store import event_append, lock, push, sync


@pytest.fixture
def tracker(tmp_path: Path) -> str:
    td = tmp_path / "trk"
    td.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "tickets", str(td)], check=True)
    subprocess.run(["git", "-C", str(td), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(td), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(td), "config", "gc.auto", "0"], check=True)
    (td / ".keep").write_text("")
    subprocess.run(["git", "-C", str(td), "add", ".keep"], check=True)
    subprocess.run(["git", "-C", str(td), "commit", "-q", "-m", "init"], check=True)
    return str(td)


def _event(**over):
    e = {
        "timestamp": 1700000000000000000,
        "uuid": "u-1",
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": "x"},
    }
    e.update(over)
    return e


# ── canonical bytes ───────────────────────────────────────────────────────────
def test_committed_bytes_are_canonical(tracker: str):
    ev = _event(data={"body": "héllo", "z": 1, "a": [3, 2, 1]})
    event_append.stage_and_commit(tracker, "tk", dict(ev))
    path = os.path.join(
        tracker, "tk", event_append.event_filename(ev["timestamp"], ev["uuid"], "COMMENT")
    )
    raw = Path(path).read_bytes()
    assert raw == json.dumps(ev, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    assert not raw.endswith(b"\n")
    # And a commit landed with the canonical message.
    msg = subprocess.run(
        ["git", "-C", tracker, "log", "-1", "--format=%s"], capture_output=True, text=True
    ).stdout.strip()
    assert msg == "ticket: COMMENT tk"


def test_filename_is_i2(tracker: str):
    assert event_append.event_filename(123, "uu", "STATUS") == "123-uu-STATUS.json"


# ── exit-code contract ────────────────────────────────────────────────────────
def test_invalid_event_type_raises_storeerror_1(tracker: str):
    with pytest.raises(event_append.StoreError) as ei:
        event_append.stage_and_commit(tracker, "tk", _event(event_type="BOGUS"))
    assert ei.value.returncode == 1
    assert "invalid event_type" in str(ei.value)


def test_event_types_all_listed_in_error_string(tracker: str):
    """Drift guard (P2.3): every EVENT_TYPES name must appear in the hand-spelled
    'invalid event_type' error string — the only place the names are enumerated."""
    with pytest.raises(event_append.StoreError) as ei:
        event_append.stage_and_commit(tracker, "tk", _event(event_type="BOGUS"))
    msg = str(ei.value)
    missing = [t for t in event_append.EVENT_TYPES if t not in msg]
    assert not missing, f"event types missing from invalid-event_type error string: {missing}"


def test_tag_delta_is_a_valid_event_type(tracker: str):
    """TAG_DELTA (P2.3) is accepted on the write path (registered in EVENT_TYPES)."""
    from rebar.reducer._version import TAG_DELTA

    rc = event_append.stage_and_commit(
        tracker, "tk", _event(event_type=TAG_DELTA, data={"added": ["x"], "removed": []})
    )
    assert rc == 0


def test_rebase_guard_exit_75(tracker: str):
    Path(tracker, ".git", "MERGE_HEAD").write_text("deadbeef\n")
    with pytest.raises(lock.RebaseGuard) as ei:
        event_append.stage_and_commit(tracker, "tk", _event())
    assert ei.value.returncode == 75
    assert "recovery state" in str(ei.value)
    # The staged temp must be cleaned up (no .tmp-event-* left behind).
    assert not [p for p in Path(tracker).iterdir() if p.name.startswith(".tmp-event-")]


def test_lock_timeout_exit_1(tracker: str):
    # Hold the lock, then a second short-budget acquire must time out (exit 1).
    held = lock.acquire(tracker, timeout=30, attempts=1)
    try:
        with pytest.raises(lock.LockTimeout) as ei:
            lock.acquire(tracker, timeout=1, attempts=1)
        assert ei.value.returncode == 1
        assert "could not acquire lock after 1s" in str(ei.value)
    finally:
        held.release()


def test_dual_lock_mutual_exclusion_same_process(tracker: str):
    h = lock.acquire(tracker, dual_window=True)
    try:
        assert Path(tracker, lock.MKDIR_LOCK_NAME).is_dir()
        # mkdir leg held → a second mkdir-leg acquire times out fast.
        with pytest.raises(lock.LockTimeout):
            lock.acquire(tracker, timeout=1, attempts=1, dual_window=True)
    finally:
        h.release()
    assert not Path(tracker, lock.MKDIR_LOCK_NAME).exists()  # released


# ── push policy parsing ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "val,expect",
    [
        ("off", "off"),
        ("OFF", "off"),
        (" Off ", "off"),
        ("async", "async"),
        ("ASYNC", "async"),
        ("always", "always"),
        ("", "always"),  # unset handled separately
    ],
)
def test_push_mode_parsing(monkeypatch, val, expect):
    monkeypatch.setenv("REBAR_PUSH", val)
    assert push._push_mode() == (expect if val.strip() else "always")


def test_push_off_is_noop(tracker: str, monkeypatch):
    monkeypatch.setenv("REBAR_PUSH", "off")
    # No remote configured; off must return immediately without error.
    push.push_tickets_branch(tracker)  # no raise


def test_push_no_remote_is_noop(tracker: str, monkeypatch):
    monkeypatch.setenv("REBAR_PUSH", "always")
    push.push_tickets_branch(tracker)  # no remote → silent best-effort return


@pytest.mark.parametrize("mod,args", [(push, ("/repo", "push")), (sync, ("/repo", "fetch"))])
def test_network_git_is_bounded_and_timeout_is_best_effort(monkeypatch, mod, args):
    """A hung network git (fetch/push) must surface as a FAILED CompletedProcess,
    never hang (bug c16f). The wrapper must pass a bounded timeout to subprocess.run
    and convert a TimeoutExpired into a non-zero return — so the best-effort callers
    (`_ok` -> False / push best-effort) degrade rather than block."""
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    cp = mod._git(*args)
    assert cp.returncode != 0, "a git timeout must surface as a failed CompletedProcess"
    assert isinstance(seen["timeout"], (int, float)) and seen["timeout"] > 0, (
        "the git wrapper must pass a bounded timeout to subprocess.run"
    )
