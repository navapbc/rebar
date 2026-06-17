"""7657 (epic 7738): isolate `session_log` from the graph/health hot paths and
from default `list`, while keeping it discoverable via `search` and `show`.

Session logs carry verbose bodies that must never tax the dependency-graph /
store-health compiles that run constantly during the parallel-agent workflow.
The contract (locked with the requester): logs appear in keyword `search` and in
single-ticket `show`, and in `list --type=session_log`, but are excluded from
default `list`, `ready`, `next_batch`, `deps`, and `validate`. These are
exercised end-to-end through the library API (which CLI + MCP funnel through), so
one assertion covers all three interfaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.interface

_KW = "zorptastic"  # distinctive keyword planted in session-log bodies


def _ids(rows: list[dict]) -> set[str]:
    return {r.get("ticket_id") for r in rows}


def _seed(repo: Path) -> dict[str, str]:
    """Seed an epic, two tasks (one depending on the epic), and two session logs."""
    r = str(repo)
    epic = rebar.create_ticket("epic", "Visibility epic", description="body", repo_root=r)
    t1 = rebar.create_ticket("task", "Task one", description="body", repo_root=r)
    t2 = rebar.create_ticket("task", "Task two", description="body", repo_root=r)
    rebar.link(t1, epic, "depends_on", repo_root=r)
    log1 = rebar.create_ticket(
        "session_log", "Log alpha", description=f"verbose {_KW} aaa", repo_root=r
    )
    log2 = rebar.create_ticket(
        "session_log", "Log beta", description=f"more {_KW} bbb", repo_root=r
    )
    return {"epic": epic, "t1": t1, "t2": t2, "log1": log1, "log2": log2}


# ── default list hides logs; explicit --type surfaces them ─────────────────────
def test_default_list_omits_session_logs(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    listed = _ids(rebar.list_tickets(repo_root=str(rebar_repo)))
    assert ids["log1"] not in listed and ids["log2"] not in listed
    # non-log tickets are unaffected
    assert ids["epic"] in listed and ids["t1"] in listed and ids["t2"] in listed


def test_typed_list_returns_only_session_logs(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    listed = _ids(rebar.list_tickets(ticket_type="session_log", repo_root=str(rebar_repo)))
    assert listed == {ids["log1"], ids["log2"]}


# ── search + show keep logs discoverable ───────────────────────────────────────
def test_search_matches_session_log_bodies(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    hits = _ids(rebar.search(_KW, repo_root=str(rebar_repo)))
    assert ids["log1"] in hits and ids["log2"] in hits


def test_show_returns_session_log_fully(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    state = rebar.show_ticket(ids["log1"], repo_root=str(rebar_repo))
    assert state["ticket_id"] == ids["log1"]
    assert state["ticket_type"] == "session_log"
    assert _KW in state["description"]


# ── graph / health hot paths never include logs ────────────────────────────────
def test_ready_excludes_session_logs(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    ready = _ids(rebar.ready(repo_root=str(rebar_repo)))
    assert ids["log1"] not in ready and ids["log2"] not in ready


def test_next_batch_excludes_session_logs(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    batch = rebar.next_batch(ids["epic"], repo_root=str(rebar_repo))
    batch_ids = {row.get("id") for row in batch.get("batch", [])}
    assert ids["log1"] not in batch_ids and ids["log2"] not in batch_ids


def test_deps_excludes_session_logs_from_graph_nodes(rebar_repo: Path) -> None:
    """session_logs are never graph *nodes* (children/blockers). A `relates_to`
    link the user explicitly created legitimately shows in the epic's `deps`
    link-list (non-blocking relations are permitted for logs) — but the log must
    never be traversed as a blocker or child node in the dependency graph."""
    ids = _seed(rebar_repo)
    rebar.link(ids["log1"], ids["epic"], "relates_to", repo_root=str(rebar_repo))
    graph = rebar.deps(ids["epic"], repo_root=str(rebar_repo))
    node_ids = {n.get("ticket_id") for n in graph.get("blockers", [])} | {
        n.get("ticket_id") for n in graph.get("children", [])
    }
    assert ids["log1"] not in node_ids and ids["log2"] not in node_ids


def test_validate_does_not_flag_session_logs(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    report = rebar.validate(repo_root=str(rebar_repo))
    blob = repr(report)
    # No health finding references a session_log ticket (orphan/empty/etc.).
    assert ids["log1"] not in blob and ids["log2"] not in blob


# ── perf isolation (AC): graph/ready outputs are invariant to log count/size ────
def test_ready_count_invariant_to_session_logs(rebar_repo: Path) -> None:
    """ready() output must be identical whether or not N verbose logs are present."""
    _seed(rebar_repo)
    before = _ids(rebar.ready(repo_root=str(rebar_repo)))
    # Add many large session logs; the graph/health result must not change.
    big = "x" * 5000
    for i in range(25):
        rebar.create_ticket(
            "session_log", f"Bulk log {i}", description=f"{big} {_KW}", repo_root=str(rebar_repo)
        )
    after = _ids(rebar.ready(repo_root=str(rebar_repo)))
    assert before == after
