"""TicketQuery — the collapsed list_tickets filter set (item toll-clock-tier / Part B).

Proves (a) TicketQuery.from_library owns the library→engine normalization, (b) the
filter core list_states accepts a TicketQuery and filters by it, and (c) the three
PUBLIC scalar boundaries still expose every filter parameter (so the collapse of the
inner layers did not narrow the public API).
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import rebar
from rebar._engine_support.ticket_query import TicketQuery


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "--quiet"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "Test"),
        ("config", "commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    return path


# The canonical filter dimensions the whole chain threads. A new filter adds one
# field here and exposes it on the three public boundaries below — the inner layers
# (_reads.list_tickets → list_states → apply_ticket_filters) forward the query.
_FILTER_FIELDS = {
    "status",
    "ticket_type",
    "priority",
    "parent",
    "has_tag",
    "without_tag",
    "include_archived",
    "exclude_deleted",
    "min_children",
    "blocking_state",
    "with_children_count",
    "sort",
}


def test_from_library_normalizes_none_and_priority():
    # None scalars collapse to the "" engine sentinel; priority int → str.
    q = TicketQuery.from_library()
    assert q == TicketQuery()  # all defaults
    assert q.status == "" and q.parent == "" and q.sort == ""
    assert q.priority == ""

    q2 = TicketQuery.from_library(status="open", priority=0, parent=None, sort="created")
    assert q2.status == "open"
    assert q2.priority == "0"  # int coerced to str
    assert q2.parent == ""  # None → ""
    assert q2.sort == "created"
    assert q2.include_body is True  # default preserved


def test_from_library_string_priority_passthrough():
    assert TicketQuery.from_library(priority="1,2").priority == "1,2"


def test_ticketquery_is_frozen():
    q = TicketQuery()
    try:
        q.status = "open"  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001 — frozen dataclass raises FrozenInstanceError
        assert "assign" in str(exc).lower() or "frozen" in type(exc).__name__.lower()
    else:  # pragma: no cover
        raise AssertionError("TicketQuery must be frozen")


def test_list_states_filters_by_query(tmp_path):
    """The collapsed filter core accepts a TicketQuery and narrows by it."""
    from rebar._engine_support import reads

    _git_repo(tmp_path)
    rebar.init_repo(repo_root=tmp_path)
    a = rebar.create_ticket("task", "alpha task", repo_root=tmp_path)
    rebar.create_ticket("bug", "beta bug", repo_root=tmp_path)
    tracker = reads.tracker_dir(tmp_path)

    # No query → everything (both tickets).
    everything = reads.list_states(tracker)
    assert len({t["ticket_id"] for t in everything}) == 2

    # Query narrows to the task type only.
    tasks = reads.list_states(tracker, TicketQuery(ticket_type="task"))
    assert [t["ticket_id"] for t in tasks] == [a]

    # include_body=False drops the bulky fields (lean list shape).
    lean = reads.list_states(tracker, TicketQuery(ticket_type="task", include_body=False))
    assert "description" not in lean[0] and "comments" not in lean[0]
    full = reads.list_states(tracker, TicketQuery(ticket_type="task", include_body=True))
    assert "description" in full[0]


def test_public_scalar_boundaries_still_expose_all_filters():
    """The 3 public scalar signatures kept every filter param (the collapse of the
    inner layers did not narrow the library API)."""
    lib_params = set(inspect.signature(rebar.list_tickets).parameters)
    missing = _FILTER_FIELDS - lib_params
    assert not missing, f"rebar.list_tickets dropped filters: {missing}"
    # `full` is the library spelling of include_body; assert it survived too.
    assert "full" in lib_params
