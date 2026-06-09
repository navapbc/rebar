"""Behavioral parity matrix: each operation × {library, CLI, MCP}.

Every parametrized test runs the same scenario through one interface and asserts
the same observable result. A separate cross-interface test writes through one
interface and reads through the other two, proving all three share one store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters import CliAdapter, LibraryAdapter, McpAdapter


@pytest.fixture(params=["library", "cli", "mcp"])
def adapter(request: pytest.FixtureRequest, rebar_repo: Path):
    """An interface adapter, constructed after rebar_repo has set REBAR_ROOT."""
    if request.param == "library":
        return LibraryAdapter()
    if request.param == "cli":
        return CliAdapter()
    pytest.importorskip("mcp")
    return McpAdapter()


def _ttype(t: dict) -> str:
    return t.get("type") or t.get("ticket_type") or ""


def test_create_show_parity(adapter) -> None:
    tid = adapter.create("task", "Parity ticket")
    assert tid and len(tid) >= 4
    t = adapter.show(tid)
    assert t["status"] == "open"
    assert _ttype(t) == "task"
    assert t["title"] == "Parity ticket"


def test_list_and_filter_parity(adapter) -> None:
    a = adapter.create("task", "Open one")
    b = adapter.create("bug", "Open two")
    all_open = adapter.list(status="open")
    ids = {t["ticket_id"] for t in all_open}
    assert {a, b} <= ids
    bugs = adapter.list(ticket_type="bug")
    assert b in {t["ticket_id"] for t in bugs}
    assert a not in {t["ticket_id"] for t in bugs}


def test_transition_happy_parity(adapter) -> None:
    tid = adapter.create("task", "To progress")
    assert adapter.transition(tid, "open", "in_progress") is True
    assert adapter.show(tid)["status"] == "in_progress"


def test_transition_stale_rejected_parity(adapter) -> None:
    """A valid-but-stale current_status is rejected and the store is unchanged
    (engine exit-10 contract surfaced uniformly across interfaces)."""
    tid = adapter.create("task", "Stale guard")
    # Ticket is 'open'; claim a valid-but-wrong current status.
    assert adapter.transition(tid, "in_progress", "closed") is False
    assert adapter.show(tid)["status"] == "open"


def test_claim_happy_parity(adapter) -> None:
    """claim moves an open ticket to in_progress and sets the assignee, identically
    across library/CLI/MCP."""
    tid = adapter.create("task", "Claimable")
    assert adapter.claim(tid, assignee="alice") is True
    state = adapter.show(tid)
    assert state["status"] == "in_progress"
    assert state.get("assignee") == "alice"


def test_claim_not_open_rejected_parity(adapter) -> None:
    """Claiming a non-open ticket is rejected (exit-10 / ConcurrencyError / MCP
    tool error) and the store is unchanged — surfaced uniformly across interfaces."""
    tid = adapter.create("task", "Already claimed")
    assert adapter.claim(tid, assignee="alice") is True
    # Second claim must be rejected; assignee must remain the first winner's.
    assert adapter.claim(tid, assignee="bob") is False
    state = adapter.show(tid)
    assert state["status"] == "in_progress"
    assert state.get("assignee") == "alice"


def test_search_parity(adapter) -> None:
    """Full-text search returns identical matches via library/CLI/MCP."""
    hit = adapter.create("task", "searchable kumquat ticket")
    adapter.create("task", "unrelated noise")
    results = adapter.search("kumquat")
    ids = {t["ticket_id"] for t in results}
    assert ids == {hit}


def test_tag_and_comment_parity(adapter) -> None:
    tid = adapter.create("task", "Tag me")
    adapter.tag(tid, "area:api")
    adapter.comment(tid, "a human note")
    t = adapter.show(tid)
    assert "area:api" in t.get("tags", [])
    assert any("human note" in (c.get("body", "")) for c in t.get("comments", []))


def test_deps_parity(adapter) -> None:
    a = adapter.create("task", "Blocker")
    b = adapter.create("task", "Blocked")
    adapter.link(a, b, "blocks")
    graph = adapter.deps(b)
    assert isinstance(graph, dict)


# ── Cross-interface coherence: one store, three windows ──────────────────────
def test_write_one_read_all(rebar_repo: Path) -> None:
    """Create via the library; both CLI and MCP must observe the same ticket,
    and a transition via MCP must be visible to the library and CLI."""
    pytest.importorskip("mcp")
    lib, cli, mcp = LibraryAdapter(), CliAdapter(), McpAdapter()

    tid = lib.create("story", "Shared across interfaces")

    for reader in (lib, cli, mcp):
        t = reader.show(tid)
        assert t["title"] == "Shared across interfaces", reader.name
        assert t["status"] == "open", reader.name

    # Mutate through MCP; library + CLI must see it.
    assert mcp.transition(tid, "open", "in_progress") is True
    assert lib.show(tid)["status"] == "in_progress"
    assert cli.show(tid)["status"] == "in_progress"
