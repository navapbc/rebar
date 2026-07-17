"""Creation-channel attribution matrix (story 6fe2, epic jira-reb-977).

Every local ingress (CLI, MCP, Python library) must stamp the interface that
produced the genesis CREATE event into an immutable ``creation_channel`` field:
persisted in ``CREATE.data`` and projected into compiled ticket state. Values are
the closed enum ``cli / mcp / python / jira / import / unknown``. New local writers
reject out-of-enum values and the projection-only ``unknown``; a legacy CREATE with
no field provisionally projects ``unknown`` and never carries
``creation_channel_inferred``. Genesis provenance is immutable — no later event may
replace it.

Observable oracle only: persisted ``CREATE.data`` bytes, the show/list/search/export
state projection, the public JSON schema, generated types, and the local-contract
docs. No internal structure is asserted.

``-k`` selectors: cli, mcp, python, invalid, legacy, immutable, projections.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

# The interface adapters live at the tests/interfaces root (put on sys.path by the
# tier conftest); ``_unwrap`` normalises a FastMCP call_tool result to a plain value.
from adapters import _unwrap  # noqa: E402

import rebar

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA_DIR = Path(rebar.__file__).resolve().parent / "schemas"
_ENUM = ["cli", "mcp", "python", "jira", "import", "unknown"]
_U1 = "11111111-1111-4111-8111-111111111111"
_U2 = "22222222-2222-4222-8222-222222222222"


# ── helpers ──────────────────────────────────────────────────────────────────
def _create_data(rebar_repo: Path, ticket_id: str) -> dict:
    """The persisted CREATE event ``data`` dict for a ticket (immutable genesis)."""
    tracker = rebar_repo / ".tickets-tracker"
    matches = sorted(tracker.glob(f"{ticket_id}/*-CREATE.json"))
    assert len(matches) == 1, f"expected one CREATE for {ticket_id}, got {matches}"
    return json.loads(matches[0].read_bytes())["data"]


def _extract_id(stdout: str) -> str:
    """Last non-empty stdout line, unwrapped from a ``{"id": ...}`` JSON line if present."""
    last = [ln for ln in stdout.splitlines() if ln.strip()][-1].strip()
    try:
        obj = json.loads(last)
        return obj["id"] if isinstance(obj, dict) and "id" in obj else last
    except (json.JSONDecodeError, TypeError):
        return last


def _cli_id(*args: str) -> str:
    # REBAR_ROOT is set by the rebar_repo fixture on os.environ; the subprocess inherits it.
    cp = subprocess.run([sys.executable, "-m", "rebar.cli", *args], capture_output=True, text=True)
    assert cp.returncode == 0, f"cli {args} failed: {cp.stderr}"
    return _extract_id(cp.stdout)


def _mcp(tool: str, **args) -> object:
    pytest.importorskip("mcp")
    from rebar.mcp_server import build_server

    return _unwrap(asyncio.run(build_server().call_tool(tool, args)))


def _mcp_id(tool: str, **args) -> str:
    res = _mcp(tool, **args)
    return res["id"] if isinstance(res, dict) and "id" in res else res


def _show_channel(rebar_repo: Path, ticket_id: str) -> str:
    return rebar.show_ticket(ticket_id, repo_root=str(rebar_repo)).get("creation_channel")


def _assert_channel(rebar_repo: Path, ticket_id: str, expected: str) -> None:
    """Both the persisted CREATE.data AND the projected state report ``expected``."""
    assert _create_data(rebar_repo, ticket_id).get("creation_channel") == expected
    assert _show_channel(rebar_repo, ticket_id) == expected


# ── python (AC3): direct library helpers default to `python` ──────────────────
def test_python_create_ticket_records_python(rebar_repo: Path):
    tid = rebar.create_ticket("task", "py task", repo_root=str(rebar_repo))
    _assert_channel(rebar_repo, tid, "python")


def test_python_idea_records_python(rebar_repo: Path):
    tid = rebar.idea("py idea", repo_root=str(rebar_repo))
    _assert_channel(rebar_repo, tid, "python")


def test_python_create_identity_records_python(rebar_repo: Path):
    tid = rebar.create_identity("py ident", "py@example.com", repo_root=str(rebar_repo))
    _assert_channel(rebar_repo, tid, "python")


def test_python_append_session_log_records_python(rebar_repo: Path):
    res = rebar.append_session_log("py entry", summary="py log", repo_root=str(rebar_repo))
    tid = res["id"] if isinstance(res, dict) else res
    _assert_channel(rebar_repo, tid, "python")


def test_python_start_session_log_records_python(rebar_repo: Path):
    res = rebar.start_session_log(summary="py start log", repo_root=str(rebar_repo))
    tid = res["id"] if isinstance(res, dict) else res
    _assert_channel(rebar_repo, tid, "python")


# ── cli (AC1): the CLI boundary declares `cli` ────────────────────────────────
def test_cli_create_records_cli(rebar_repo: Path):
    tid = _cli_id("create", "task", "cli task")
    _assert_channel(rebar_repo, tid, "cli")


def test_cli_idea_records_cli(rebar_repo: Path):
    tid = _cli_id("idea", "cli idea")
    _assert_channel(rebar_repo, tid, "cli")


def test_cli_identity_records_cli(rebar_repo: Path):
    tid = _cli_id("identity", "create", "--name", "cli ident", "--email", "cli@example.com")
    _assert_channel(rebar_repo, tid, "cli")


def test_cli_session_log_records_cli(rebar_repo: Path):
    tid = _cli_id("session-log", "append", "cli entry")
    _assert_channel(rebar_repo, tid, "cli")


# ── mcp (AC2): the MCP boundary declares `mcp` ────────────────────────────────
def test_mcp_create_ticket_records_mcp(rebar_repo: Path):
    tid = _mcp_id("create_ticket", ticket_type="task", title="mcp task")
    _assert_channel(rebar_repo, tid, "mcp")


def test_mcp_create_idea_records_mcp(rebar_repo: Path):
    tid = _mcp_id("create_idea", title="mcp idea")
    _assert_channel(rebar_repo, tid, "mcp")


def test_mcp_create_identity_records_mcp(rebar_repo: Path):
    tid = _mcp_id("create_identity", name="mcp ident", email="mcp@example.com")
    _assert_channel(rebar_repo, tid, "mcp")


def test_mcp_log_session_records_mcp(rebar_repo: Path):
    res = _mcp("log_session", entry="mcp entry", summary="mcp log")
    tid = res["id"] if isinstance(res, dict) else res
    _assert_channel(rebar_repo, tid, "mcp")


# ── invalid (AC4a): the core rejects off-vocabulary and the projection-only value
# The channel is never user-supplied (every boundary hard-codes it), so an out-of-enum
# value is an internal contract violation surfaced as ValueError, not a user CommandError.
def test_invalid_channel_rejected(rebar_repo: Path):
    from rebar._commands import composer

    with pytest.raises(ValueError, match="must be one of"):
        composer.create_core("task", "bad", creation_channel="github", repo_root=str(rebar_repo))


def test_invalid_unknown_channel_rejected_for_live_write(rebar_repo: Path):
    from rebar._commands import composer

    # `unknown` is a projection-only fallback, never a valid live-write channel.
    with pytest.raises(ValueError, match="projection-only"):
        composer.create_core("task", "bad", creation_channel="unknown", repo_root=str(rebar_repo))


# ── legacy (AC4b): a channel-less CREATE reduces to `unknown`, no inference marker
def test_legacy_create_without_channel_reduces_to_unknown(tmp_path: Path):
    from rebar.reducer import reduce_ticket

    tdir = tmp_path / "legacy"
    tdir.mkdir()
    event = {
        "event_type": "CREATE",
        "uuid": _U1,
        "timestamp": 1700000000,
        "author": "someone",
        "env_id": "someenv",
        "data": {"id": "legacyid00000001", "ticket_type": "task", "title": "legacy", "priority": 2},
    }
    (tdir / f"1700000000-{_U1}-CREATE.json").write_text(json.dumps(event))
    state = reduce_ticket(str(tdir))
    assert state["creation_channel"] == "unknown"
    assert "creation_channel_inferred" not in state


# ── immutable (AC5): no later event replaces genesis provenance ───────────────
def test_immutable_edit_cannot_replace_genesis_creation_channel(tmp_path: Path):
    from rebar.reducer import reduce_ticket

    tdir = tmp_path / "tkt"
    tdir.mkdir()
    create = {
        "event_type": "CREATE",
        "uuid": _U1,
        "timestamp": 1700000000,
        "author": "a",
        "data": {
            "id": "tid0000000000001",
            "ticket_type": "task",
            "title": "orig",
            "priority": 2,
            "creation_channel": "python",
        },
    }
    edit = {
        "event_type": "EDIT",
        "uuid": _U2,
        "timestamp": 1700000001,
        "author": "a",
        "data": {"fields": {"creation_channel": "cli", "title": "changed"}},
    }
    (tdir / f"1700000000-{_U1}-CREATE.json").write_text(json.dumps(create))
    (tdir / f"1700000001-{_U2}-EDIT.json").write_text(json.dumps(edit))
    state = reduce_ticket(str(tdir))
    # The EDIT's immutable-field payload is skipped, but a normal field still applies.
    assert state["creation_channel"] == "python", "genesis channel must be immutable"
    assert state["title"] == "changed", "non-immutable EDIT fields must still apply"


# ── projections (AC6): schema, types, live outputs, and docs expose the contract
def test_projections_common_schema_defines_six_value_enum():
    schema = json.loads((_SCHEMA_DIR / "common.schema.json").read_bytes())
    defs = schema.get("$defs", schema.get("definitions", {}))
    node = defs.get("creation_channel", {})
    assert node.get("enum") == _ENUM, f"creation_channel enum must be {_ENUM}"


def test_projections_ticket_state_schema_has_channel_and_const_marker():
    schema = json.loads((_SCHEMA_DIR / "ticket_state.schema.json").read_bytes())
    props = schema["properties"]
    assert "creation_channel" in props
    # The marker must carry const:true (a description alongside it is fine).
    assert props["creation_channel_inferred"].get("const") is True, (
        "the inference marker must be encoded with const: true"
    )
    # Additive: neither field is required.
    assert "creation_channel" not in schema.get("required", [])
    assert "creation_channel_inferred" not in schema.get("required", [])


def test_projections_show_list_search_retain_channel(rebar_repo: Path):
    tid = rebar.create_ticket("task", "proj retain", repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["creation_channel"] == "python"
    listed = {t["ticket_id"]: t for t in rebar.list_tickets(repo_root=str(rebar_repo))}
    assert listed[tid]["creation_channel"] == "python"
    hits = {t["ticket_id"]: t for t in rebar.search("proj retain", repo_root=str(rebar_repo))}
    assert hits[tid]["creation_channel"] == "python"


def test_projections_export_ndjson_retains_channel(rebar_repo: Path, tmp_path: Path):
    tid = rebar.create_ticket("task", "proj export", repo_root=str(rebar_repo))
    out = tmp_path / "export.ndjson"
    rebar.export_tickets(out=str(out), repo_root=str(rebar_repo))
    rows = {}
    for line in out.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row.get("ticket_id", row.get("id"))] = row
    assert rows[tid].get("creation_channel") == "python"


def test_projections_generated_types_expose_channel_literal():
    types_src = (Path(rebar.__file__).resolve().parent / "types.py").read_text()
    assert "creation_channel" in types_src
    # The {"const": true} marker must render as Literal[True], not Any.
    assert "creation_channel_inferred" in types_src
    assert "Literal[True]" in types_src


def test_projections_docs_document_creation_channel_enum():
    for rel in ("docs/event-schema.md", "docs/output-schemas.md"):
        text = (_REPO_ROOT / rel).read_text()
        assert "creation_channel" in text, f"{rel} must document creation_channel"
        assert "creation_channel_inferred" in text, f"{rel} must document the inference marker"
        for value in _ENUM:
            assert value in text, f"{rel} must document channel value '{value}'"
