"""`idea` status vocabulary + dispatch-exclusion contract (story 81b4).

`idea` is a first-class ticket status for captured-but-undesigned work. This module
pins the observable behaviour of the base story through the library surface over one
shared store (``REBAR_ROOT`` set by the ``rebar_repo`` fixture in conftest.py):

- `idea` is a legal ``transition`` current/target status (free transitions, no rigid
  state machine): ``open→idea``, ``idea→open``, ``idea→in_progress`` all succeed.
- `list --status=idea` returns exactly the `idea` tickets (and excludes non-`idea`).
- `idea` tickets NEVER surface in ``ready`` or ``next-batch`` (excluded by omission).
- `search` still returns matching `idea` tickets (the search path is not
  status-filtered), so a parked idea can always be found and promoted later.
"""

from __future__ import annotations

from pathlib import Path

import rebar


def test_transition_open_to_idea_and_back(rebar_repo: Path) -> None:
    """`idea` is a legal target and current status: open→idea→open→in_progress."""
    tid = rebar.create_ticket("task", "A rough idea", repo_root=str(rebar_repo))

    r1 = rebar.transition(tid, "open", "idea", repo_root=str(rebar_repo))
    assert r1["to"] == "idea"

    r2 = rebar.transition(tid, "idea", "open", repo_root=str(rebar_repo))
    assert r2["to"] == "open"

    # And promotion straight from idea into active work is legal too.
    rebar.transition(tid, "open", "idea", repo_root=str(rebar_repo))
    r3 = rebar.transition(tid, "idea", "in_progress", repo_root=str(rebar_repo))
    assert r3["to"] == "in_progress"


def test_list_status_idea_filters_precisely(rebar_repo: Path) -> None:
    """`list --status=idea` returns the idea ticket and excludes an open one."""
    idea_id = rebar.create_ticket("task", "Parked idea", repo_root=str(rebar_repo))
    open_id = rebar.create_ticket("task", "Active work", repo_root=str(rebar_repo))
    rebar.transition(idea_id, "open", "idea", repo_root=str(rebar_repo))

    ids = {t["ticket_id"] for t in rebar.list_tickets(status="idea", repo_root=str(rebar_repo))}
    assert idea_id in ids
    assert open_id not in ids


def test_ready_excludes_idea(rebar_repo: Path) -> None:
    """An `idea` ticket must never appear in `ready` (undesigned → not dispatchable)."""
    idea_id = rebar.create_ticket("task", "Parked idea", repo_root=str(rebar_repo))
    open_id = rebar.create_ticket("task", "Active work", repo_root=str(rebar_repo))
    rebar.transition(idea_id, "open", "idea", repo_root=str(rebar_repo))

    ids = {t["ticket_id"] for t in rebar.ready(repo_root=str(rebar_repo))}
    assert open_id in ids  # unblocked open → ready
    assert idea_id not in ids  # idea → excluded


def test_next_batch_excludes_idea(rebar_repo: Path) -> None:
    """An `idea` child must never be selected into an epic's next-batch."""
    epic = rebar.create_ticket("epic", "Epic", repo_root=str(rebar_repo))
    active = rebar.create_ticket("task", "Active child", parent=epic, repo_root=str(rebar_repo))
    parked = rebar.create_ticket("task", "Parked child", parent=epic, repo_root=str(rebar_repo))
    rebar.transition(parked, "open", "idea", repo_root=str(rebar_repo))

    result = rebar.next_batch(epic, repo_root=str(rebar_repo))
    batch_ids = {item["id"] for item in result.get("batch", [])}
    assert active in batch_ids
    assert parked not in batch_ids


def test_search_returns_idea_tickets(rebar_repo: Path) -> None:
    """`search` is NOT status-filtered: a parked idea is still findable by keyword."""
    idea_id = rebar.create_ticket("task", "Zephyr telemetry parking lot", repo_root=str(rebar_repo))
    rebar.transition(idea_id, "open", "idea", repo_root=str(rebar_repo))

    ids = {t["ticket_id"] for t in rebar.search("Zephyr", repo_root=str(rebar_repo))}
    assert idea_id in ids
