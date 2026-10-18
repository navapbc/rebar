"""Close metadata is cleared when a winning STATUS event leaves ``closed`` (bug 6df4-d3f0).

Before the fix, ``_fold_close_metadata`` only ever wrote the four close keys, so a forced
close followed by reopen left ``force_close_reason`` on an open ticket, and a later clean
close kept reporting the old gate bypass.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.reducer import _cache, reduce_ticket
from rebar.reducer import _processors_status as processors_status
from rebar.reducer._cache import compute_dir_hash, write_cache

pytestmark = pytest.mark.unit

_CLOSE_KEYS = ("close_class", "close_reason", "force_close_reason", "completion_expectation")
_FORCED_CLOSE = {
    "force_close_reason": "bypass",
    "close_class": "regression",
    "close_reason": "why",
    "completion_expectation": "expected",
}


def _apply(state: dict, uuid: str, current: str | None, target: str, **data) -> dict:
    event = {"uuid": uuid, "env_id": "e", "timestamp": 1}
    payload = {"current_status": current, "status": target, **data}
    processors_status.process_status(state, event, payload, "")
    return state


def _force_closed_state() -> dict:
    return _apply({"status": "open", "ticket_id": "t"}, "u1", "open", "closed", **_FORCED_CLOSE)


def test_reopen_clears_all_close_metadata() -> None:
    state = _apply(_force_closed_state(), "u2", "closed", "open")
    assert state["status"] == "open"
    assert [k for k in _CLOSE_KEYS if k in state] == []


def test_clean_close_after_reopen_reports_no_bypass() -> None:
    state = _apply(_force_closed_state(), "u2", "closed", "open")
    _apply(state, "u3", "open", "closed")
    assert state["status"] == "closed"
    assert "force_close_reason" not in state


def test_losing_reopen_fork_keeps_the_winning_close_metadata() -> None:
    """A concurrent reopen that loses the fork never reaches the clearing step."""
    state = _apply({"status": "open", "ticket_id": "t"}, "u5", "open", "closed", **_FORCED_CLOSE)
    # Stale expected status forms a fork; its larger UUID loses to the recorded winner u5.
    _apply(state, "u9", "in_progress", "open")
    assert state["status"] == "closed"
    assert state["force_close_reason"] == "bypass"


def test_winning_reopen_fork_clears_close_metadata() -> None:
    """A reopen that wins a fork also clears the close keys (the fork-winner branch)."""
    # Recorded status differs from the event's expected ``closed``, so the event forks;
    # its UUID u1 sorts before the recorded winner u9, so it wins.
    state = {"status": "in_progress", "ticket_id": "t", "parent_status_uuid": "u9"}
    state.update(_FORCED_CLOSE)
    _apply(state, "u1", "closed", "open")
    assert state["status"] == "open"
    assert state["status_fork_resolutions"][-1]["winner_uuid"] == "u1"
    assert [k for k in _CLOSE_KEYS if k in state] == []


def test_losing_close_fork_does_not_overwrite_the_winner() -> None:
    state = _apply({"status": "open", "ticket_id": "t"}, "u5", "open", "closed", close_class="a")
    _apply(state, "u9", "in_progress", "closed", close_class="b", force_close_reason="late")
    assert state["close_class"] == "a"
    assert "force_close_reason" not in state


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _status_events(tdir: str) -> list[dict]:
    names = sorted(n for n in os.listdir(tdir) if n.endswith("-STATUS.json"))
    return [json.loads(Path(tdir, n).read_text()).get("data") or {} for n in names]


def test_end_to_end_force_close_reopen_clean_close(store: Path) -> None:
    tid = rebar.create_ticket("task", "forced then remediated", repo_root=str(store))
    tdir = str(store / ".tickets-tracker" / tid)
    rebar.transition(tid, "open", "closed", force="repro bypass", repo_root=str(store))
    assert reduce_ticket(tdir)["force_close_reason"]

    rebar.reopen(tid, repo_root=str(store))
    reopened = reduce_ticket(tdir)
    assert reopened["status"] == "open"
    assert [k for k in _CLOSE_KEYS if k in reopened] == []

    rebar.transition(tid, "open", "closed", repo_root=str(store))
    closed = reduce_ticket(tdir)
    assert closed["status"] == "closed"
    assert "force_close_reason" not in closed

    # The event log keeps the forced close for forensics.
    forced = [e for e in _status_events(tdir) if e.get("force_close_reason")]
    assert len(forced) == 1


def test_stale_cache_from_previous_reducer_version_is_recomputed(store: Path) -> None:
    tid = rebar.create_ticket("task", "cached stale bypass", repo_root=str(store))
    tdir = str(store / ".tickets-tracker" / tid)
    rebar.transition(tid, "open", "closed", force="repro bypass", repo_root=str(store))
    rebar.reopen(tid, repo_root=str(store))
    events = sorted(n for n in os.listdir(tdir) if n.endswith(".json") and not n.startswith("."))

    # Forge the cache version 7 would have written: an open ticket still carrying the bypass.
    saved = _cache._REDUCER_CACHE_VERSION
    try:
        _cache._REDUCER_CACHE_VERSION = 7
        old_hash = compute_dir_hash(tdir, events)
    finally:
        _cache._REDUCER_CACHE_VERSION = saved
    stale = {**reduce_ticket(tdir), "force_close_reason": "repro bypass"}
    write_cache(str(Path(tdir) / ".cache.json"), old_hash, stale, tdir)

    assert "force_close_reason" not in reduce_ticket(tdir)
