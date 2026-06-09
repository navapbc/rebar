"""Unit tests for rebar_reconciler/outbound_differ.py.

Tests the outbound differ that compares local ticket state against Jira
snapshots and emits OutboundMutation objects for changes to push to Jira.

Uses the importlib spec_from_file_location pattern established in the
reconciler test tree (see conftest.py docstring for rationale).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ", OUTBOUND_DIFFER_PATH)


# ---------------------------------------------------------------------------
# Stub BindingStore
# ---------------------------------------------------------------------------


class StubBindingStore:
    """In-memory binding store for tests."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        # bindings: {local_id: jira_key}
        self._bindings: dict[str, str] = bindings or {}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticket(
    ticket_id: str = "abc-1234",
    title: str = "Fix the widget",
    description: str = "It is broken",
    status: str = "open",
    priority: int = 2,
    ticket_type: str = "bug",
    assignee: str = "alice",
    tags: list[str] | None = None,
    comments: list[dict] | None = None,
    deps: list[str] | None = None,
) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "ticket_type": ticket_type,
        "assignee": assignee,
        "tags": tags or [],
        "comments": comments or [],
        "deps": deps or [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unbound_ticket_emits_create(outbound_differ: ModuleType) -> None:
    """Local ticket with no binding -> outbound create with all fields."""
    ticket = _make_ticket(ticket_id="local-1", title="New feature")
    store = StubBindingStore()  # no bindings

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
    )

    assert len(result) == 1
    m = result[0]
    assert m.local_id == "local-1"
    assert m.jira_key is None
    assert m.action == "create"
    assert m.fields["summary"] == "New feature"
    assert m.fields["issuetype"] == "Bug"
    assert m.fields["priority"] == "Medium"
    assert m.fields["status"] == "To Do"


def test_bound_ticket_no_changes_emits_nothing(
    outbound_differ: ModuleType,
) -> None:
    """Bound ticket whose fields match Jira -> no mutation."""
    ticket = _make_ticket(
        ticket_id="local-1",
        title="Fix the widget",
        description="It is broken",
        status="open",
        priority=2,
        ticket_type="bug",
        assignee="alice",
    )
    store = StubBindingStore({"local-1": "PROJ-100"})
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Fix the widget",
            "description": "It is broken",
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
        }
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    assert result == []


def test_bound_ticket_none_assignee_does_not_emit_update(
    outbound_differ: ModuleType,
) -> None:
    """Bound ticket with assignee=None matches Jira assignee="" — no update.

    Regression for live-probe finding (field-probe-1779984990): when a
    ticket is created with the new "unassigned by default" behavior, the
    ticket reducer stores ``assignee: None``. The outbound differ's
    ``ticket.get("assignee", "")`` returned None (not the "" default)
    because .get's default only applies when the key is MISSING — not
    when it's present with value None. None != "" then flagged the field
    as changed, and the update payload propagated assignee=None through
    update_one → client.update_issue(assignee=None) → ACLI received
    "--assignee None" (literal string) → exit 1, killing the pass.

    Fix: outbound_differ._map_local_to_jira_fields now uses
    ``.get("assignee") or ""`` which normalises None to "".
    """
    ticket = _make_ticket(
        ticket_id="local-1",
        title="Fix the widget",
        description="It is broken",
        status="open",
        priority=2,
        ticket_type="bug",
    )
    # Override: simulate the unassigned default (None instead of a string)
    ticket["assignee"] = None
    # Same for description: a freshly-created ticket without -d may carry None.
    ticket["description"] = None
    store = StubBindingStore({"local-1": "PROJ-100"})
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Fix the widget",
            "description": "",  # Jira has no description set
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",  # Jira is unassigned
            "labels": [],
        }
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    assert result == [], (
        f"None assignee/description must normalise to '' for comparison. "
        f"Got mutations: {result}. Without the fix, the differ flags "
        f"None != '' as a field change and propagates None to "
        f"client.update_issue, which str()'s it to 'None' for ACLI."
    )


def test_bound_ticket_field_change_emits_update(
    outbound_differ: ModuleType,
) -> None:
    """Bound ticket with title change -> update with only changed fields."""
    ticket = _make_ticket(
        ticket_id="local-1",
        title="Updated title",
        status="open",
        priority=2,
        ticket_type="bug",
        assignee="alice",
        description="It is broken",
    )
    store = StubBindingStore({"local-1": "PROJ-100"})
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Original title",
            "description": "It is broken",
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
        }
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    assert len(result) == 1
    m = result[0]
    assert m.action == "update"
    assert m.jira_key == "PROJ-100"
    assert m.fields == {"summary": "Updated title"}


def test_archived_ticket_excluded(outbound_differ: ModuleType) -> None:
    """Ticket with status=archived -> no mutation emitted."""
    ticket = _make_ticket(ticket_id="local-1", status="archived")
    store = StubBindingStore()

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
    )

    assert result == []


def test_deleted_ticket_excluded(outbound_differ: ModuleType) -> None:
    """Ticket with status=deleted -> no mutation emitted."""
    ticket = _make_ticket(ticket_id="local-1", status="deleted")
    store = StubBindingStore()

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
    )

    assert result == []


def test_label_diff_excludes_dso_id(outbound_differ: ModuleType) -> None:
    """dso-id-* labels are not included in label diff mutations."""
    ticket = _make_ticket(
        ticket_id="local-1",
        tags=["dso-id-local-1", "real-label"],
        status="open",
    )
    store = StubBindingStore({"local-1": "PROJ-100"})
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Fix the widget",
            "description": "It is broken",
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": ["dso-id-local-1"],
        }
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    assert len(result) == 1
    m = result[0]
    # Only the real-label should appear as an add; dso-id-* filtered out
    label_adds = [lb for lb in m.labels if lb["action"] == "add"]
    label_removes = [lb for lb in m.labels if lb["action"] == "remove"]
    assert len(label_adds) == 1
    assert label_adds[0]["label"] == "real-label"
    assert label_removes == []


def test_label_add_and_remove(outbound_differ: ModuleType) -> None:
    """Local has tag that Jira doesn't, and vice versa."""
    ticket = _make_ticket(
        ticket_id="local-1",
        tags=["local-only", "shared"],
        status="open",
    )
    store = StubBindingStore({"local-1": "PROJ-100"})
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Fix the widget",
            "description": "It is broken",
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": ["jira-only", "shared"],
        }
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    assert len(result) == 1
    m = result[0]
    label_adds = [lb for lb in m.labels if lb["action"] == "add"]
    label_removes = [lb for lb in m.labels if lb["action"] == "remove"]
    assert any(lb["label"] == "local-only" for lb in label_adds)
    assert any(lb["label"] == "jira-only" for lb in label_removes)


def test_priority_mapping(outbound_differ: ModuleType) -> None:
    """Local int priority 0 -> 'Highest', 4 -> 'Lowest'."""
    ticket_highest = _make_ticket(ticket_id="t1", priority=0)
    ticket_lowest = _make_ticket(ticket_id="t2", priority=4)
    store = StubBindingStore()

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket_highest, ticket_lowest],
        jira_snapshot={},
        binding_store=store,
    )

    assert len(result) == 2
    fields_by_id = {m.local_id: m.fields for m in result}
    assert fields_by_id["t1"]["priority"] == "Highest"
    assert fields_by_id["t2"]["priority"] == "Lowest"


def test_status_mapping(outbound_differ: ModuleType) -> None:
    """Local 'open' -> 'To Do', 'closed' -> 'Done'."""
    ticket_open = _make_ticket(ticket_id="t1", status="open")
    ticket_closed = _make_ticket(ticket_id="t2", status="closed")
    store = StubBindingStore()

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket_open, ticket_closed],
        jira_snapshot={},
        binding_store=store,
        excluded_statuses={"archived", "deleted"},  # closed is NOT excluded
    )

    fields_by_id = {m.local_id: m.fields for m in result}
    assert fields_by_id["t1"]["status"] == "To Do"
    assert fields_by_id["t2"]["status"] == "Done"
