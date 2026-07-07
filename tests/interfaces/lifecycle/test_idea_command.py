"""Atomic `idea` command across CLI / MCP / library (story 2918).

An idea must be captured in ONE race-free operation: born directly in status `idea`
via a single CREATE event (no intervening STATUS event), so it is never momentarily
`open`/claimable. The command always creates an `epic` from a title (+ optional
description); there is deliberately NO general `create --status` flag.

Covers: the reducer honoring an optional CREATE `status` (default `open`); the three
surfaces (CLI `rebar idea`, MCP `create_idea`, library `rebar.idea`) each producing a
single-CREATE `epic` in `idea`; and the end-to-end user flow (excluded from
ready/next-batch → promote via `idea→open` → reject via `idea→closed`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import rebar
from rebar.reducer._processors import process_create
from rebar.reducer._state import make_initial_state


def _events(repo: Path, tid: str) -> list[str]:
    """Event-type names for a ticket, from its raw event-log filenames."""
    d = repo / ".tickets-tracker" / tid
    return sorted(
        p.name.split("-")[-1].removesuffix(".json") for p in d.glob("*.json") if "-" in p.name
    )


def _create_events(repo: Path, tid: str) -> list[str]:
    return [e for e in _events(repo, tid) if e in ("CREATE", "STATUS")]


# ── reducer: process_create honors optional status ────────────────────────────
def test_process_create_honors_status_when_present():
    state = make_initial_state()
    ev = {"author": "me", "timestamp": 1, "env_id": "t"}
    data = {"ticket_type": "epic", "title": "T", "status": "idea"}
    process_create(state, ev, data, "abcd-0000-0000-0000", "/tmp/nope.json", "h")
    assert state["status"] == "idea"


def test_process_create_defaults_status_open_when_absent():
    state = make_initial_state()
    ev = {"author": "me", "timestamp": 1, "env_id": "t"}
    data = {"ticket_type": "task", "title": "T"}  # no status
    process_create(state, ev, data, "abcd-0000-0000-0001", "/tmp/nope.json", "h")
    assert state["status"] == "open"


# ── library: rebar.idea ───────────────────────────────────────────────────────
def test_library_idea_creates_epic_idea_single_create(rebar_repo: Path):
    tid = rebar.idea("A rough idea", description="notes", repo_root=str(rebar_repo))
    t = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert t["ticket_type"] == "epic"
    assert t["status"] == "idea"
    assert t["description"] == "notes"
    # Single genesis: exactly one CREATE and no STATUS event.
    assert _create_events(rebar_repo, tid) == ["CREATE"]


# ── CLI: rebar idea ───────────────────────────────────────────────────────────
def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_cli_idea_creates_epic_idea_single_create(rebar_repo: Path):
    cp = _cli("idea", "CLI idea", "--description=cli notes", cwd=str(rebar_repo))
    assert cp.returncode == 0, cp.stderr
    # No file-impact nudge on stderr (unlike `create`).
    assert "file_impact" not in cp.stderr

    ideas = rebar.list_tickets(status="idea", repo_root=str(rebar_repo))
    tid = next(t["ticket_id"] for t in ideas if t["title"] == "CLI idea")
    t = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert t["ticket_type"] == "epic" and t["status"] == "idea"
    assert t["description"] == "cli notes"
    assert _create_events(rebar_repo, tid) == ["CREATE"]


def test_cli_has_no_general_create_status_flag(rebar_repo: Path):
    """`create --status=idea` must NOT be honored: idea is the sole non-open genesis."""
    _cli("create", "task", "Sneaky", "--status=idea", cwd=str(rebar_repo))
    # Either the flag is rejected, or it is ignored — but it must never yield status=idea.
    ideas = {t["title"] for t in rebar.list_tickets(status="idea", repo_root=str(rebar_repo))}
    assert "Sneaky" not in ideas, "create must not accept a --status flag to mint an idea"


# ── MCP: create_idea ──────────────────────────────────────────────────────────
def test_mcp_create_idea(rebar_repo: Path):
    import asyncio

    import pytest

    pytest.importorskip("mcp")
    from rebar.mcp_server import build_server

    srv = build_server()
    result = asyncio.run(srv.call_tool("create_idea", {"title": "MCP idea"}))
    # call_tool returns (content, structured) or content; extract the id from structured.
    structured = result[1] if isinstance(result, tuple) else result
    tid = structured["id"] if isinstance(structured, dict) else structured.get("id")

    t = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert t["ticket_type"] == "epic" and t["status"] == "idea"
    assert _create_events(rebar_repo, tid) == ["CREATE"]


# ── end-to-end user flow ──────────────────────────────────────────────────────
def test_idea_end_to_end_flow(rebar_repo: Path):
    tid = rebar.idea("Lifecycle idea", repo_root=str(rebar_repo))

    # Absent from ready and next-batch (undesigned → not dispatchable).
    assert tid not in {t["ticket_id"] for t in rebar.ready(repo_root=str(rebar_repo))}
    nb = rebar.next_batch(tid, repo_root=str(rebar_repo))
    assert tid not in {i["id"] for i in nb.get("batch", [])}

    # Promote to real work.
    rebar.transition(tid, "idea", "open", repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"

    # Or reject: idea → closed succeeds with no completion-gate error.
    tid2 = rebar.idea("Rejected idea", repo_root=str(rebar_repo))
    out = rebar.transition(tid2, "idea", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"
