"""Status annotations preserve blocked and cancelled across Jira’s smaller workflow. Outbound
maps workflow status and manages rebar-status labels; inbound prefers annotations, while
tag sync excludes them."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "outbound_differ.py"
INBOUND_DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "inbound_differ.py"


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return importlib.import_module("rebar.reconciler.inbound_differ")


# ---------------------------------------------------------------------------
# Stub BindingStores
# ---------------------------------------------------------------------------


class StubOutboundBindingStore:
    """In-memory binding store for outbound tests."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


class StubInboundBindingStore:
    """In-memory binding store for inbound tests."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        # bindings: {jira_key: local_id}
        self._bindings: dict[str, str] = bindings or {}

    def get_local_id(self, jira_key: str) -> str | None:
        return self._bindings.get(jira_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_local_ticket(
    ticket_id: str = "local-1",
    status: str = "open",
    title: str = "Test ticket",
    description: str = "desc",
    priority: int = 2,
    ticket_type: str = "task",
    assignee: str = "",
    tags: list[str] | None = None,
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
        "comments": [],
        "deps": [],
    }


def _make_jira_fields(
    status: str = "In Progress",
    summary: str = "Test ticket",
    description: str = "desc",
    priority: str = "Medium",
    assignee: str = "",
    labels: list[str] | None = None,
) -> dict:
    return {
        "summary": summary,
        "description": description,
        "issuetype": "Task",
        "priority": priority,
        "status": status,
        "assignee": assignee,
        "labels": labels or [],
    }


# ---------------------------------------------------------------------------
# Test (a): outbound blocked → "In Progress" + rebar-status:blocked label intent
# ---------------------------------------------------------------------------


def test_outbound_blocked_maps_to_in_progress(outbound_differ: ModuleType) -> None:
    """Blocked maps to Jira In Progress rather than an unavailable Blocked workflow state."""
    ticket = _make_local_ticket(ticket_id="local-1", status="blocked")
    store = StubOutboundBindingStore()  # unbound → create mutation

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
    )

    assert len(result) == 1, f"Expected 1 create mutation, got {result}"
    m = result[0]
    assert m.action == "create"
    assert m.fields["status"] == "In Progress", (
        f"blocked must map to 'In Progress' (live DIG workflow), got: {m.fields['status']!r}"
    )


def test_outbound_blocked_emits_rebar_status_blocked_label(outbound_differ: ModuleType) -> None:
    """(a) outbound blocked must also emit rebar-status:blocked label intent."""
    ticket = _make_local_ticket(ticket_id="local-1", status="blocked")
    store = StubOutboundBindingStore()

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
    )

    assert len(result) == 1
    m = result[0]
    label_names = {lb["label"] for lb in m.labels if lb.get("action") == "add"}
    assert "rebar-status:blocked" in label_names, (
        f"outbound blocked must emit rebar-status:blocked label. Got labels: {m.labels}"
    )


def test_outbound_blocked_update_maps_to_in_progress(outbound_differ: ModuleType) -> None:
    """(a) bound ticket with status=blocked → update with status='In Progress'."""
    ticket = _make_local_ticket(ticket_id="local-1", status="blocked", title="Fix me")
    store = StubOutboundBindingStore({"local-1": "DIG-100"})
    jira_snapshot = {
        "DIG-100": _make_jira_fields(status="In Progress", summary="Fix me", labels=[]),
    }

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    # Should emit update with rebar-status:blocked label (since Jira doesn't have it yet)
    assert len(result) == 1
    m = result[0]
    assert m.action == "update"
    # Status in the changed fields should be "In Progress" (not "Blocked")
    if "status" in m.fields:
        assert m.fields["status"] == "In Progress", (
            f"blocked must map to 'In Progress', got: {m.fields['status']!r}"
        )
    # rebar-status:blocked label must be added
    label_adds = [lb["label"] for lb in m.labels if lb.get("action") == "add"]
    assert "rebar-status:blocked" in label_adds, (
        f"Must emit rebar-status:blocked label for blocked status. Got: {m.labels}"
    )


# ---------------------------------------------------------------------------
# Test (b): outbound cancelled → "Done" + rebar-status:cancelled label intent
# ---------------------------------------------------------------------------


def test_outbound_cancelled_maps_to_done(outbound_differ: ModuleType) -> None:
    """Cancelled maps to Jira Done rather than an unavailable Cancelled workflow state."""
    ticket = _make_local_ticket(ticket_id="local-1", status="cancelled")
    store = StubOutboundBindingStore()

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        config=outbound_differ.OutboundDiffConfig(
            excluded_statuses={"archived", "deleted"},  # cancelled is NOT excluded
        ),
    )

    assert len(result) == 1, f"Expected 1 create mutation, got {result}"
    m = result[0]
    assert m.action == "create"
    assert m.fields["status"] == "Done", (
        f"cancelled must map to 'Done' (live DIG workflow), got: {m.fields['status']!r}"
    )


def test_outbound_cancelled_emits_rebar_status_cancelled_label(outbound_differ: ModuleType) -> None:
    """(b) outbound cancelled must also emit rebar-status:cancelled label intent."""
    ticket = _make_local_ticket(ticket_id="local-1", status="cancelled")
    store = StubOutboundBindingStore()

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        config=outbound_differ.OutboundDiffConfig(
            excluded_statuses={"archived", "deleted"},
        ),
    )

    assert len(result) == 1
    m = result[0]
    label_names = {lb["label"] for lb in m.labels if lb.get("action") == "add"}
    assert "rebar-status:cancelled" in label_names, (
        f"outbound cancelled must emit rebar-status:cancelled label. Got labels: {m.labels}"
    )


# ---------------------------------------------------------------------------
# Test (c): status change blocked→in_progress removes rebar-status:blocked label
# ---------------------------------------------------------------------------


def test_outbound_blocked_to_in_progress_removes_annotation_label(
    outbound_differ: ModuleType,
) -> None:
    """Leaving blocked removes its rebar-status:blocked annotation exactly once."""
    ticket = _make_local_ticket(
        ticket_id="local-1",
        status="in_progress",
        title="Fixed now",
    )
    store = StubOutboundBindingStore({"local-1": "DIG-100"})
    # Jira still has the old annotation label from when it was blocked
    jira_snapshot = {
        "DIG-100": _make_jira_fields(
            status="In Progress",
            summary="Fixed now",
            labels=["rebar-status:blocked"],
        ),
    }

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    assert len(result) == 1, f"Expected 1 update mutation, got {result}"
    m = result[0]
    label_removes = [lb["label"] for lb in m.labels if lb.get("action") == "remove"]
    assert "rebar-status:blocked" in label_removes, (
        f"Must remove stale rebar-status:blocked when status moves to in_progress. "
        f"Got labels: {m.labels}"
    )


def test_outbound_cancelled_to_closed_removes_annotation_label(
    outbound_differ: ModuleType,
) -> None:
    """(c) variant: when closed replaces cancelled, remove rebar-status:cancelled."""
    ticket = _make_local_ticket(
        ticket_id="local-1",
        status="closed",
        title="All done",
    )
    store = StubOutboundBindingStore({"local-1": "DIG-200"})
    jira_snapshot = {
        "DIG-200": _make_jira_fields(
            status="Done",
            summary="All done",
            labels=["rebar-status:cancelled"],
        ),
    }

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    assert len(result) == 1, f"Expected 1 update mutation, got {result}"
    m = result[0]
    label_removes = [lb["label"] for lb in m.labels if lb.get("action") == "remove"]
    assert "rebar-status:cancelled" in label_removes, (
        f"Must remove stale rebar-status:cancelled when status moves to closed. "
        f"Got labels: {m.labels}"
    )


# ---------------------------------------------------------------------------
# Test (d): inbound rebar-status:blocked label → local "blocked"
# ---------------------------------------------------------------------------


def test_inbound_rebar_status_blocked_label_overrides_jira_status(
    inbound_differ: ModuleType,
) -> None:
    """An inbound rebar-status:blocked label overrides Jira’s In Progress workflow status."""
    jira_snapshot = {
        "DIG-100": _make_jira_fields(
            status="In Progress",
            summary="Blocked ticket",
            labels=["rebar-status:blocked"],
        ),
    }
    store = StubInboundBindingStore({"DIG-100": "local-1"})
    local_tickets = {
        "local-1": _make_local_ticket(
            ticket_id="local-1",
            status="blocked",
            title="Blocked ticket",
        ),
    }

    mutations, _ = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    # Local is already "blocked"; Jira says "In Progress" but has rebar-status:blocked.
    # No status change should be emitted (local is correct).
    status_changes = [m for m in mutations if m.local_id == "local-1" and "status" in m.fields]
    assert status_changes == [], (
        f"Inbound must NOT overwrite local 'blocked' when rebar-status:blocked is present. "
        f"Got: {status_changes}"
    )


def test_inbound_rebar_status_blocked_label_sets_local_blocked_when_local_is_open(
    inbound_differ: ModuleType,
) -> None:
    """(d) inbound rebar-status:blocked label → local status 'blocked', not 'in_progress'."""
    jira_snapshot = {
        "DIG-100": _make_jira_fields(
            status="In Progress",
            summary="Some ticket",
            labels=["rebar-status:blocked"],
        ),
    }
    store = StubInboundBindingStore({"DIG-100": "local-1"})
    local_tickets = {
        "local-1": _make_local_ticket(
            ticket_id="local-1",
            status="open",  # local is open; Jira says In Progress + label
            title="Some ticket",
        ),
    }

    mutations, _ = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    status_mutations = [m for m in mutations if m.local_id == "local-1" and "status" in m.fields]
    assert len(status_mutations) == 1, f"Expected 1 status mutation. Got: {mutations}"
    assert status_mutations[0].fields["status"] == "blocked", (
        f"inbound with rebar-status:blocked label must set status='blocked', "
        f"got: {status_mutations[0].fields['status']!r}"
    )


def test_inbound_rebar_status_cancelled_label_sets_local_cancelled(
    inbound_differ: ModuleType,
) -> None:
    """(d) inbound rebar-status:cancelled label → local status = 'cancelled'."""
    jira_snapshot = {
        "DIG-200": _make_jira_fields(
            status="Done",
            summary="Cancelled ticket",
            labels=["rebar-status:cancelled"],
        ),
    }
    store = StubInboundBindingStore({"DIG-200": "local-2"})
    local_tickets = {
        "local-2": _make_local_ticket(
            ticket_id="local-2",
            status="open",
            title="Cancelled ticket",
        ),
    }

    mutations, _ = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    status_mutations = [m for m in mutations if m.local_id == "local-2" and "status" in m.fields]
    assert len(status_mutations) == 1
    assert status_mutations[0].fields["status"] == "cancelled", (
        f"inbound with rebar-status:cancelled must set status='cancelled', "
        f"got: {status_mutations[0].fields['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test (e): inbound "In Review" with no rebar-status label → "in_progress"
# ---------------------------------------------------------------------------


def test_inbound_in_review_maps_to_in_progress(inbound_differ: ModuleType) -> None:
    """Jira In Review without an annotation maps to local in_progress."""
    jira_snapshot = {
        "DIG-300": _make_jira_fields(
            status="In Review",
            summary="Under review",
            labels=[],
        ),
    }
    store = StubInboundBindingStore({"DIG-300": "local-3"})
    local_tickets = {
        "local-3": _make_local_ticket(
            ticket_id="local-3",
            status="open",
            title="Under review",
        ),
    }

    mutations, _ = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    status_mutations = [m for m in mutations if m.local_id == "local-3" and "status" in m.fields]
    assert len(status_mutations) == 1, (
        f"Expected 1 status mutation for 'In Review' → 'in_progress'. Got: {mutations}"
    )
    assert status_mutations[0].fields["status"] == "in_progress", (
        f"'In Review' must map to 'in_progress', got: {status_mutations[0].fields['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test (f): rebar-status: labels excluded from local tag sync both directions
# ---------------------------------------------------------------------------


def test_outbound_rebar_status_labels_excluded_from_tag_sync(
    outbound_differ: ModuleType,
) -> None:
    """Outbound tag diff ignores reconciler-managed rebar-status labels and must not
    duplicate status-driven removal."""
    # Local is now in_progress (not blocked), no rebar-status: tag in local tags
    ticket = _make_local_ticket(
        ticket_id="local-1",
        status="in_progress",
        title="Fixed",
        tags=["user-tag"],  # only user tags
    )
    store = StubOutboundBindingStore({"local-1": "DIG-100"})
    jira_snapshot = {
        "DIG-100": _make_jira_fields(
            status="In Progress",
            summary="Fixed",
            labels=["rebar-status:blocked", "user-tag"],
        ),
    }

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    # Status logic removes rebar-status:blocked once; generic tag diff must not duplicate it.
    if result:
        m = result[0]
        label_removes = [lb["label"] for lb in m.labels if lb.get("action") == "remove"]
        # The remove should appear exactly once (not duplicated by tag diff)
        remove_count = label_removes.count("rebar-status:blocked")
        assert remove_count <= 1, (
            f"rebar-status:blocked removal should appear at most once (not duplicated). "
            f"Got {remove_count} removes."
        )


def test_inbound_rebar_status_labels_excluded_from_local_tag_sync(
    inbound_differ: ModuleType,
) -> None:
    """Inbound label sync does not add a reconciler-managed rebar-status annotation to user
    tags."""
    jira_snapshot = {
        "DIG-100": _make_jira_fields(
            status="In Progress",
            summary="Blocked ticket",
            labels=["rebar-status:blocked", "real-user-tag"],
        ),
    }
    store = StubInboundBindingStore({"DIG-100": "local-1"})
    local_tickets = {
        "local-1": _make_local_ticket(
            ticket_id="local-1",
            status="blocked",
            title="Blocked ticket",
            tags=[],  # no rebar-status: tags locally
        ),
    }

    mutations, _ = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    # Collect all inbound label adds for local-1
    label_adds = []
    for m in mutations:
        if m.local_id == "local-1":
            label_adds.extend(lb["label"] for lb in m.labels if lb.get("action") == "add")

    assert "rebar-status:blocked" not in label_adds, (
        f"rebar-status:blocked must NOT be added as a local user tag. Got label adds: {label_adds}"
    )
    # real-user-tag should still come through
    assert "real-user-tag" in label_adds, (
        f"real-user-tag must still be synced inbound. Got: {label_adds}"
    )
