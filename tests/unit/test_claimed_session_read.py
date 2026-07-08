"""Read surface + compaction safety for claimed_session (story 199b / S3).

Covers: show JSON presence, the MCP show_ticket outputSchema (TicketStateOut), the
to_llm compact rendering, the process_snapshot compaction round-trip (post- AND
pre-feature snapshots), and the docs/event-schema.md documentation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar.reducer import make_initial_state
from rebar.reducer._processors import process_snapshot
from rebar.reducer.llm_format import to_llm

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ show JSON
def test_show_json_includes_claimed_session(tmp_path, monkeypatch) -> None:
    for var in ("REBAR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    monkeypatch.setenv("REBAR_SESSION_ID", "read-sess")
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    rebar.claim(tid, assignee="alice", repo_root=str(repo))
    shown = rebar.show_ticket(tid, repo_root=str(repo))
    assert "claimed_session" in shown
    assert shown["claimed_session"] == "read-sess"


def test_show_json_unclaimed_ticket_has_null_session(tmp_path, monkeypatch) -> None:
    """Contract for the unclaimed case: claimed_session is present as null (key-present, per
    the make_initial_state default), never omitted — so consumers get a defined value."""
    for var in ("REBAR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    shown = rebar.show_ticket(tid, repo_root=str(repo))
    assert "claimed_session" in shown
    assert shown["claimed_session"] is None


# ------------------------------------------------------------------ MCP outputSchema
def test_outputschema_advertises_claimed_session() -> None:
    models = pytest.importorskip("rebar._mcp_models")
    ticket_state_out = models.TicketStateOut
    if ticket_state_out is None:  # pydantic unavailable in this env
        pytest.skip("pydantic not installed")
    props = ticket_state_out.model_json_schema()["properties"]
    assert "claimed_session" in props


# ------------------------------------------------------------------ to_llm
def test_to_llm_renders_csn_when_set() -> None:
    state = make_initial_state()
    state.update({"ticket_id": "t", "ticket_type": "task", "claimed_session": "sess-1"})
    out = to_llm(state)
    assert out.get("csn") == "sess-1"
    assert "claimed_session" not in out  # rendered under the short key only


def test_to_llm_omits_csn_when_none() -> None:
    state = make_initial_state()
    state.update({"ticket_id": "t", "ticket_type": "task", "claimed_session": None})
    out = to_llm(state)
    assert "csn" not in out


# ------------------------------------------------------------------ compaction round-trip
def test_compaction_preserves_claimed_session() -> None:
    """A post-feature SNAPSHOT carries claimed_session in compiled_state -> restored verbatim."""
    state = make_initial_state()
    snapshot_data = {"compiled_state": {"status": "in_progress", "claimed_session": "sess-snap"}}
    process_snapshot(state, snapshot_data)
    assert state["claimed_session"] == "sess-snap"


def test_compaction_pre_feature_snapshot_defaults_none() -> None:
    """A pre-feature SNAPSHOT lacks claimed_session; the restore loop leaves the
    make_initial_state seed (None) intact — key-present, no error."""
    state = make_initial_state()
    assert state["claimed_session"] is None  # seeded before replay
    pre_feature = {"compiled_state": {"status": "in_progress", "assignee": "alice"}}
    process_snapshot(state, pre_feature)
    assert "claimed_session" in state
    assert state["claimed_session"] is None


# ------------------------------------------------------------------ docs
def test_event_schema_documents_claimed_session() -> None:
    """Anchor on the dedicated section so 'compaction' can't false-positive on an unrelated
    cross-reference elsewhere in the file (advisory E6)."""
    doc = (_REPO_ROOT / "docs" / "event-schema.md").read_text(encoding="utf-8")
    marker = "## Session provenance (`claimed_session`)"
    assert marker in doc, "dedicated claimed_session section header must exist"
    section = doc.split(marker, 1)[1].split("\n## ", 1)[0]
    for required in ("claimed_session", 'data["session"]', "compaction"):
        assert required in section, f"section must document {required!r}"


def test_llm_schema_documents_csn() -> None:
    """The LLM output schema (validation oracle for --output llm) enumerates the csn alias."""
    import json

    schema = json.loads(
        (_REPO_ROOT / "src" / "rebar" / "schemas" / "ticket_state_llm.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "csn" in schema["properties"]
    assert "claimed_session→csn" in schema["description"]
