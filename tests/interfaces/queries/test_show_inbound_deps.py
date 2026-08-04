"""`rebar show` surfaces computed INBOUND edges (bug 05cb-f7af-2d85-4d60).

A LINK event is stored one-sided on the SOURCE ticket's record, so for
`A --blocks--> B` the edge historically appeared only in `show A` — `show B`
looked unblocked while `ready`/`next-batch` correctly withheld it. These tests
pin the additive fix: `show` renders a NEW `inbound_deps` key (list of
`{"from_id", "relation", "status"}`, meaning "from_id <relation> this
ticket") derived at read time, on the CLI default output and over MCP, without
changing any existing key (`deps` stays the outgoing-only stored list).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import rebar


def _cli_show(tid: str, cwd: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "show", tid],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _blocks_pair(repo: Path) -> tuple[str, str]:
    """Two tickets with `A --blocks--> B` (edge stored on A only)."""
    r = str(repo)
    a = rebar.create_ticket("task", "Blocker A", repo_root=r)
    b = rebar.create_ticket("task", "Blocked B", repo_root=r)
    rebar.link(a, b, "blocks", repo_root=r)
    return a, b


def test_cli_show_surfaces_inbound_blocker(rebar_repo: Path) -> None:
    """`show B` makes the inbound `blocks` edge visible; existing keys and the
    outgoing side are unchanged — direction is unambiguous on both records."""
    a, b = _blocks_pair(rebar_repo)

    show_b = _cli_show(b, str(rebar_repo))
    inbound = show_b["inbound_deps"]
    assert [(e["from_id"], e["relation"]) for e in inbound] == [(a, "blocks")]
    assert inbound[0]["status"] == "open"
    # Additive: the stored (outgoing) deps list is untouched.
    assert show_b["deps"] == []

    # The blocker's own view is unchanged outgoing + no inbound.
    show_a = _cli_show(a, str(rebar_repo))
    assert [(d["relation"], d["target_id"]) for d in show_a["deps"]] == [("blocks", b)]
    assert show_a["inbound_deps"] == []


def test_cli_show_unblocked_ticket_has_no_inbound(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Standalone", repo_root=str(rebar_repo))
    assert _cli_show(tid, str(rebar_repo))["inbound_deps"] == []


def test_mcp_show_ticket_surfaces_inbound_blocker(rebar_repo: Path) -> None:
    """The MCP `show_ticket` tool exposes the same additive `inbound_deps`."""
    import asyncio

    from adapters import McpAdapter, _unwrap  # noqa: F401  (adapters on sys.path via conftest)

    a, b = _blocks_pair(rebar_repo)
    mcp = McpAdapter()
    show_b = mcp.show(b)
    assert [(e["from_id"], e["relation"]) for e in show_b["inbound_deps"]] == [(a, "blocks")]
    assert show_b["deps"] == []
    assert mcp.show(a)["inbound_deps"] == []

    # The advertised outputSchema declares the new key (additive-only change).
    tools = asyncio.run(mcp._srv.list_tools())
    out_schema = next(t for t in tools if t.name == "show_ticket").outputSchema
    assert "inbound_deps" in out_schema.get("properties", {})


def test_show_and_ready_cannot_disagree_on_blockedness(rebar_repo: Path) -> None:
    """Same fixture, both tools: `ready` withholds B (blocked) and keeps A;
    `show` now reports exactly the same picture — an unclosed inbound blocker
    on B, none on A — so a reader of a single `show` reaches the conclusion
    `ready` computes."""
    a, b = _blocks_pair(rebar_repo)
    r = str(rebar_repo)

    ready_ids = {t["ticket_id"] for t in rebar.ready(repo_root=r)}
    assert a in ready_ids
    assert b not in ready_ids

    def unclosed_inbound_blockers(tid: str) -> list[str]:
        return [
            e["from_id"]
            for e in _cli_show(tid, r)["inbound_deps"]
            if e["relation"] in ("blocks", "depends_on") and e["status"] not in ("closed",)
        ]

    assert unclosed_inbound_blockers(b) == [a]
    assert unclosed_inbound_blockers(a) == []

    # Close the blocker: ready admits B, and show's inbound entry now reads
    # closed — the two views move together.
    rebar.claim(a, assignee="me", repo_root=r)
    rebar.transition(a, "in_progress", "closed", repo_root=r)
    assert b in {t["ticket_id"] for t in rebar.ready(repo_root=r)}
    assert unclosed_inbound_blockers(b) == []
    entry = _cli_show(b, r)["inbound_deps"][0]
    assert (entry["from_id"], entry["status"]) == (a, "closed")


def test_inbound_generalises_to_other_relations(rebar_repo: Path) -> None:
    """The fix is general, not special-cased to `blocks`: a `discovered_from`
    inbound edge is visible on the target too, direction-labelled."""
    r = str(rebar_repo)
    parent = rebar.create_ticket("task", "Parent work", repo_root=r)
    found = rebar.create_ticket("task", "Discovered work", repo_root=r)
    rebar.link(found, parent, "discovered_from", repo_root=r)

    show_parent = _cli_show(parent, r)
    assert [(e["from_id"], e["relation"]) for e in show_parent["inbound_deps"]] == [
        (found, "discovered_from")
    ]
