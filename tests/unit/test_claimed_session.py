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
from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir
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
    return reduce_ticket(layout_ticket_dir(tracker_dir(str(repo)), tid))


def _status_events(tid: str, repo: Path) -> list[dict]:
    ticket_dir = Path(layout_ticket_dir(tracker_dir(str(repo)), tid))
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


# ------------------------------------------------------------------ clear on exit (S1: 6f74)
def _enter_event(uuid: str, session: str, harness: str, remote: str) -> dict:
    """A winning ``open -> in_progress`` claim that stamps all three provenance fields."""
    return {
        "uuid": uuid,
        "env_id": "env",
        "timestamp": 1,
        "data": {
            "status": "in_progress",
            "current_status": "open",
            "parent_status_uuid": "p0",
            "session": session,
            "harness": harness,
            "remote_session": remote,
        },
    }


def _exit_event(uuid: str, target: str, parent_uuid: str) -> dict:
    """An ``in_progress -> <target>`` exit STATUS event."""
    return {
        "uuid": uuid,
        "env_id": "env",
        "timestamp": 2,
        "data": {
            "status": target,
            "current_status": "in_progress",
            "parent_status_uuid": parent_uuid,
        },
    }


def _reduce_enter_then_exit(target: str) -> dict:
    state = make_initial_state()
    state["status"] = "open"
    state["parent_status_uuid"] = "p0"
    enter = _enter_event("u1", "A", "claude-code", "R")
    process_status(state, enter, enter["data"], "")
    exit_ev = _exit_event("u2", target, "u1")
    process_status(state, exit_ev, exit_ev["data"], "")
    return state


def test_claim_session_cleared_on_close() -> None:
    """All three claim-session provenance fields clear on ``in_progress -> closed`` (happy
    path + collateral invariants)."""
    state = _reduce_enter_then_exit("closed")
    assert state["claimed_session"] is None
    assert state["claim_harness"] is None
    assert state["claim_remote_session"] is None


@pytest.mark.parametrize("target", ["blocked", "open", "idea"])
def test_claim_session_cleared_on_exit(target: str) -> None:
    """The three-field clear holds for every non-``in_progress`` exit target."""
    state = _reduce_enter_then_exit(target)
    assert state["claimed_session"] is None
    assert state["claim_harness"] is None
    assert state["claim_remote_session"] is None


def test_claim_session_persists_while_in_progress() -> None:
    """Negative control: no exit -> the holder persists, proving the change clears only on
    exit from ``in_progress``, never on entry."""
    state = make_initial_state()
    state["status"] = "open"
    state["parent_status_uuid"] = "p0"
    enter = _enter_event("u1", "A", "claude-code", "R")
    process_status(state, enter, enter["data"], "")
    assert state["claimed_session"] == "A"
    assert state["claim_harness"] == "claude-code"
    assert state["claim_remote_session"] == "R"


def test_fork_winning_exit_clears_holder() -> None:
    """In a STATUS fork, a WINNING ``in_progress -> exit`` event clears the holder (the clear
    is applied on the fork-winner branch, mirroring ``_fold_claimed_session`` gating)."""
    state = make_initial_state()
    state["status"] = "blocked"  # a losing sibling already advanced the live status
    state["parent_status_uuid"] = "ffff-loser"
    state["claimed_session"] = "stale"
    state["claim_harness"] = "H"
    state["claim_remote_session"] = "R"
    winning_exit = {
        "uuid": "0000-winner",
        "env_id": "env",
        "timestamp": 5,
        "data": {"status": "closed", "current_status": "in_progress", "parent_status_uuid": "p0"},
    }
    process_status(state, winning_exit, winning_exit["data"], "")
    assert state["status"] == "closed"
    assert state["claimed_session"] is None
    assert state["claim_harness"] is None
    assert state["claim_remote_session"] is None


def test_fork_losing_exit_does_not_clear_holder() -> None:
    """In a STATUS fork, a LOSING ``in_progress -> exit`` event must NOT clear the fork
    winner's holder — the clear is never applied on the existing-chain-wins branch (advisory
    G6/T8, symmetric to ``test_fork_winner_session_wins``)."""
    state = make_initial_state()
    state["status"] = "closed"  # the winning sibling already exited
    state["parent_status_uuid"] = "0000-winner"
    state["claimed_session"] = "keeper"
    state["claim_harness"] = "H"
    state["claim_remote_session"] = "R"
    losing_exit = {
        "uuid": "ffff-loser",
        "env_id": "env",
        "timestamp": 5,
        "data": {"status": "blocked", "current_status": "in_progress", "parent_status_uuid": "p0"},
    }
    process_status(state, losing_exit, losing_exit["data"], "")
    assert state["claimed_session"] == "keeper"
    assert state["claim_harness"] == "H"
    assert state["claim_remote_session"] == "R"


def test_claim_then_exit_clears_holder_e2e(rebar_repo: Path, monkeypatch) -> None:
    """End-to-end via the library: a claim stamps the holder, and a real exit transition
    clears all three provenance fields in the reduced state."""
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-e2e")
    monkeypatch.setenv("AI_AGENT", "claude-code")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))
    assert _state(tid, rebar_repo)["claimed_session"] == "sess-e2e"
    rebar.transition(tid, "in_progress", "blocked", repo_root=str(rebar_repo))
    st = _state(tid, rebar_repo)
    assert st["claimed_session"] is None
    assert st["claim_harness"] is None
    assert st["claim_remote_session"] is None
