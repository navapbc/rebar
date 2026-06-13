"""Behavioral parity matrix: each operation × {library, CLI, MCP}.

Every parametrized test runs the same scenario through one interface and asserts
the same observable result. A separate cross-interface test writes through one
interface and reads through the other two, proving all three share one store.
"""

from __future__ import annotations

import json
import subprocess
import sys
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


def _cli_list_ids(*flags: str) -> set[str]:
    """Run `rebar list <flags>` (JSON oracle) and return the matched ticket ids."""
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "list", *flags],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, f"cli list {flags} failed: {cp.stderr}"
    return {t["ticket_id"] for t in json.loads(cp.stdout)}


@pytest.mark.parametrize("exclude_deleted", [False, True])
def test_list_exclude_deleted_parity(rebar_repo: Path, exclude_deleted: bool) -> None:
    """`exclude_deleted` must exist and behave identically across CLI/library/MCP.

    delete writes STATUS(deleted)+ARCHIVED, so the DEFAULT list already hides
    tombstones via archived-exclusion; exclude_deleted only changes results when
    combined with include_archived=True. The CLI is the oracle; library and MCP
    must return the SAME ids for each flag combination.
    """
    pytest.importorskip("mcp")
    lib, mcp = LibraryAdapter(), McpAdapter()

    live = lib.create("task", "still alive")
    doomed = lib.create("task", "to be deleted")

    # Delete via the CLI (destructive; requires explicit approval).
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "delete", doomed, "--user-approved"],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, f"cli delete failed: {cp.stderr}"

    # Default list: tombstone hidden by archived-exclusion regardless of the flag.
    cli_default = _cli_list_ids()
    assert cli_default == {live}
    assert {t["ticket_id"] for t in lib.list(exclude_deleted=exclude_deleted)} == cli_default
    assert {t["ticket_id"] for t in mcp.list(exclude_deleted=exclude_deleted)} == cli_default

    # include_archived=True is where exclude_deleted actually matters.
    cli_flags = ["--include-archived"] + (["--exclude-deleted"] if exclude_deleted else [])
    cli_ids = _cli_list_ids(*cli_flags)
    expected = {live} if exclude_deleted else {live, doomed}
    assert cli_ids == expected

    lib_ids = {
        t["ticket_id"]
        for t in lib.list(include_archived=True, exclude_deleted=exclude_deleted)
    }
    mcp_ids = {
        t["ticket_id"]
        for t in mcp.list(include_archived=True, exclude_deleted=exclude_deleted)
    }
    assert lib_ids == cli_ids
    assert mcp_ids == cli_ids


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
