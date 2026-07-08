"""Multi-harness provenance: claim_harness + claim_remote_session (story c557 / S5).

Covers the resolvers, the write-stamp + reducer fold (present/absent), fork-winner +
session-less clear semantics for the new fields, the state defaults + schemas + read
surface, and the docs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar._commands.session_id import resolve_harness, resolve_remote_session
from rebar.reducer import make_initial_state, reduce_ticket
from rebar.reducer._processors import process_status
from rebar.reducer.llm_format import to_llm

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROV_VARS = (
    "REBAR_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "SESSION_ID",
    "AI_AGENT",
    "CLAUDE_CODE_REMOTE_SESSION_ID",
)


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in _PROV_VARS:
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _state(tid: str, repo: Path) -> dict:
    return reduce_ticket(str(tracker_dir(str(repo)) / tid))


# ------------------------------------------------------------------ resolvers
def test_resolve_harness(monkeypatch) -> None:
    for var in _PROV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert resolve_harness() is None
    monkeypatch.setenv("AI_AGENT", "claude-code_1.2.3")
    assert resolve_harness() == "claude-code_1.2.3"


def test_resolve_remote_session(monkeypatch) -> None:
    for var in _PROV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert resolve_remote_session() is None
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "remote-9")
    assert resolve_remote_session() == "remote-9"


# ------------------------------------------------------------------ claim records harness
def test_claim_records_harness(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_SESSION_ID", "s")
    monkeypatch.setenv("AI_AGENT", "opencode")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="a", repo_root=str(rebar_repo))
    assert _state(tid, rebar_repo)["claim_harness"] == "opencode"


def test_claim_absent_harness_is_none(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_SESSION_ID", "s")  # session present, harness absent
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="a", repo_root=str(rebar_repo))
    assert _state(tid, rebar_repo)["claim_harness"] is None


# ------------------------------------------------------------------ remote session
def test_claim_records_remote_session(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "remote-7")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="a", repo_root=str(rebar_repo))
    assert _state(tid, rebar_repo)["claim_remote_session"] == "remote-7"


# ------------------------------------------------------------------ fork-winner + clear
def _ip_event(uuid: str, harness: str) -> dict:
    return {
        "uuid": uuid,
        "env_id": "env",
        "timestamp": 1,
        "data": {
            "status": "in_progress",
            "current_status": "open",
            "parent_status_uuid": "p0",
            "session": "s-" + harness,
            "harness": harness,
            "remote_session": "r-" + harness,
        },
    }


@pytest.mark.parametrize("order", [("lo", "hi"), ("hi", "lo")])
def test_fork_winner_harness_wins(order) -> None:
    events = {"lo": _ip_event("0000-w", "winner"), "hi": _ip_event("ffff-l", "loser")}
    state = make_initial_state()
    state["status"] = "open"
    state["parent_status_uuid"] = "p0"
    for key in order:
        ev = events[key]
        process_status(state, ev, ev["data"], "")
    assert state["claim_harness"] == "winner"
    assert state["claim_remote_session"] == "r-winner"


def test_provenance_less_reclaim_clears() -> None:
    state = make_initial_state()
    state["status"] = "open"
    state["parent_status_uuid"] = "p0"
    ev1 = _ip_event("u1", "orig")
    process_status(state, ev1, ev1["data"], "")
    assert state["claim_harness"] == "orig"
    state["status"] = "open"
    ev2 = {
        "uuid": "u2",
        "env_id": "env",
        "timestamp": 2,
        "data": {"status": "in_progress", "current_status": "open", "parent_status_uuid": "u1"},
    }
    process_status(state, ev2, ev2["data"], "")
    assert state["claim_harness"] is None
    assert state["claim_remote_session"] is None


# ------------------------------------------------------------------ schema + read surface
def test_initial_state_defaults() -> None:
    s = make_initial_state()
    assert s["claim_harness"] is None
    assert s["claim_remote_session"] is None


def test_schema_enumerates_new_fields() -> None:
    state_schema = json.loads(
        (_REPO_ROOT / "src/rebar/schemas/ticket_state.schema.json").read_text(encoding="utf-8")
    )
    assert "claim_harness" in state_schema["properties"]
    assert "claim_remote_session" in state_schema["properties"]
    llm = json.loads(
        (_REPO_ROOT / "src/rebar/schemas/ticket_state_llm.schema.json").read_text(encoding="utf-8")
    )
    assert "chn" in llm["properties"]
    assert "rsn" in llm["properties"]


def test_read_surface_to_llm() -> None:
    state = make_initial_state()
    state.update(
        {
            "ticket_id": "t",
            "ticket_type": "task",
            "claim_harness": "codex",
            "claim_remote_session": "r1",
        }
    )
    out = to_llm(state)
    assert out.get("chn") == "codex"
    assert out.get("rsn") == "r1"


def test_read_surface_mcp_model() -> None:
    models = pytest.importorskip("rebar._mcp_models")
    m = models.TicketStateOut
    if m is None:
        pytest.skip("pydantic not installed")
    props = m.model_json_schema()["properties"]
    assert "claim_harness" in props
    assert "claim_remote_session" in props


# ------------------------------------------------------------------ docs
def test_docs_document_provenance() -> None:
    cfg = (_REPO_ROOT / "docs/config.md").read_text(encoding="utf-8")
    assert "AI_AGENT" in cfg
    assert "OPENCODE_SESSION_ID" in cfg
    ev = (_REPO_ROOT / "docs/event-schema.md").read_text(encoding="utf-8")
    assert "claim_harness" in ev
    assert "claim_remote_session" in ev
