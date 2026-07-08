"""Record the claiming coding-agent session id on ``open -> in_progress`` (story 68ef).

End-to-end (claim / bare transition, present + byte-identical-absent) via the library
API, plus direct reducer tests for the fork-WINNER gating (epic advisory G6/T8) and the
forward-compatible tolerance of the additive ``data["session"]`` key.
"""

from __future__ import annotations

import glob
import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar.reducer import make_initial_state, reduce_ticket
from rebar.reducer._processors import process_status

pytestmark = pytest.mark.unit

_SESSION_VARS = ("REBAR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "SESSION_ID")


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in _SESSION_VARS:
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _state(tid: str, repo: Path) -> dict:
    return reduce_ticket(str(tracker_dir(str(repo)) / tid))


def _status_events(tid: str, repo: Path) -> list[dict]:
    ticket_dir = tracker_dir(str(repo)) / tid
    out = []
    for path in sorted(glob.glob(str(ticket_dir / "*-STATUS.json"))):
        out.append(json.loads(Path(path).read_text(encoding="utf-8")))
    return out


# ------------------------------------------------------------------ claim records
def test_claim_records_claimed_session(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-xyz")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))
    assert _state(tid, rebar_repo)["claimed_session"] == "sess-xyz"


def test_claim_records_claude_code_session(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))
    assert _state(tid, rebar_repo)["claimed_session"] == "claude-1"


# ------------------------------------------------------------------ absent (byte-identical)
def test_claim_absent_session_records_nothing(rebar_repo: Path) -> None:
    """No session env var -> no claimed_session in state AND no `session` key on the event."""
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))
    assert _state(tid, rebar_repo).get("claimed_session") is None
    status_events = _status_events(tid, rebar_repo)
    in_progress = [e for e in status_events if e["data"].get("status") == "in_progress"]
    assert in_progress, "expected an in_progress STATUS event"
    for e in in_progress:
        assert "session" not in e["data"], "no-session path must omit the session key"


# ------------------------------------------------------------------ bare transition
def test_bare_transition_records_claimed_session(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-tr")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    assert _state(tid, rebar_repo)["claimed_session"] == "sess-tr"


def test_transition_cascade_records_on_parent(rebar_repo: Path, monkeypatch) -> None:
    """The parent-first cascade also stamps the session on the cascaded parent claim."""
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-cascade")
    parent = rebar.create_ticket("epic", "p", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "c", parent=parent, repo_root=str(rebar_repo))
    rebar.claim(child, assignee="bob", repo_root=str(rebar_repo))
    assert _state(child, rebar_repo)["claimed_session"] == "sess-cascade"
    assert _state(parent, rebar_repo)["claimed_session"] == "sess-cascade"


# ------------------------------------------------------------------ fork-winner gating
def _status_event(uuid: str, session: str) -> dict:
    return {
        "uuid": uuid,
        "env_id": "env",
        "timestamp": 1,
        "data": {
            "status": "in_progress",
            "current_status": "open",
            "parent_status_uuid": "p0",
            "session": session,
        },
    }


@pytest.mark.parametrize("order", [("lo", "hi"), ("hi", "lo")])
def test_fork_winner_session_wins(order) -> None:
    """Two competing open->in_progress claims: claimed_session is the lexical-UUID winner's,
    regardless of replay order — the losing claim never overwrites it (advisory G6/T8)."""
    events = {
        "lo": _status_event("0000-winner-uuid", "winner-session"),
        "hi": _status_event("ffff-loser-uuid", "loser-session"),
    }
    state = make_initial_state()
    state["status"] = "open"
    state["parent_status_uuid"] = "p0"
    for key in order:
        ev = events[key]
        process_status(state, ev, ev["data"], "")
    assert state["claimed_session"] == "winner-session"


# ------------------------------------------------------------------ stale-clear (T9)
def test_session_less_reclaim_clears_stale() -> None:
    """A later open->in_progress claim carrying NO session clears a prior claimed_session,
    so the field never mis-attributes the current episode to a past session (advisory T9)."""
    state = make_initial_state()
    state["status"] = "open"
    state["parent_status_uuid"] = "p0"
    ev1 = _status_event("u1", "old-session")
    process_status(state, ev1, ev1["data"], "")
    assert state["claimed_session"] == "old-session"
    # Simulate a fresh open->in_progress with no session stamped (session key omitted).
    state["status"] = "open"
    ev2 = {
        "uuid": "u2",
        "env_id": "env",
        "timestamp": 2,
        "data": {"status": "in_progress", "current_status": "open", "parent_status_uuid": "u1"},
    }
    process_status(state, ev2, ev2["data"], "")
    assert state["claimed_session"] is None


def test_initial_state_defaults_claimed_session_none() -> None:
    assert make_initial_state()["claimed_session"] is None


def test_non_claim_edge_leaves_claimed_session_untouched() -> None:
    """A non-`open->in_progress` edge (e.g. blocked->in_progress resume) must NOT re-fold
    claimed_session, so a resume is never mis-attributed to a new session (advisory T8)."""
    state = make_initial_state()
    state["status"] = "open"
    state["parent_status_uuid"] = "p0"
    ev1 = _status_event("u1", "orig-session")
    process_status(state, ev1, ev1["data"], "")
    assert state["claimed_session"] == "orig-session"
    # Now blocked, then blocked->in_progress with NO session key stamped (write side only
    # stamps open->in_progress): claimed_session must be untouched.
    state["status"] = "blocked"
    resume = {
        "uuid": "u2",
        "env_id": "env",
        "timestamp": 3,
        "data": {"status": "in_progress", "current_status": "blocked", "parent_status_uuid": "u1"},
    }
    process_status(state, resume, resume["data"], "")
    assert state["claimed_session"] == "orig-session"


# ------------------------------------------------------------------ forward-compat
def test_forward_compat_unknown_key_tolerated() -> None:
    """process_status folds `session` and tolerates an arbitrary unknown data key without
    error; the unknown key does not leak into state (proxy for an older clone ignoring the
    additive key)."""
    state = make_initial_state()
    state["status"] = "open"
    state["parent_status_uuid"] = "p0"
    ev = {
        "uuid": "u1",
        "env_id": "env",
        "timestamp": 1,
        "data": {
            "status": "in_progress",
            "current_status": "open",
            "parent_status_uuid": "p0",
            "session": "sess-fc",
            "some_future_key": "ignored",
        },
    }
    process_status(state, ev, ev["data"], "")
    assert state["claimed_session"] == "sess-fc"
    assert "some_future_key" not in state
