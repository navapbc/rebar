"""Unit tests for lossless status mapping via dso-status: annotation labels.

Ticket: 929a-cc1b-09ee-4e7a
DIG live workflow = {To Do, In Progress, In Review, Done} only.
blocked/cancelled must map to In Progress/Done plus a dso-status: label.
Inbound must prefer dso-status: label over raw Jira workflow status.
dso-status: labels must be excluded from local tag sync.

Test IDs:
  (a) outbound blocked → "In Progress" + dso-status:blocked label intent
  (b) outbound cancelled → "Done" + dso-status:cancelled label intent
  (c) status change blocked→in_progress removes dso-status:blocked label
  (d) inbound dso-status:blocked label → local "blocked" regardless of workflow status
  (e) inbound "In Review" with no label → "in_progress" (not "open")
  (f) dso-status: labels excluded from local tag sync both directions
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
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "outbound_differ.py"
)
INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "inbound_differ.py"
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
    return _load_module("outbound_differ_status_ann", OUTBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return _load_module("inbound_differ_status_ann", INBOUND_DIFFER_PATH)


# ---------------------------------------------------------------------------
# Stub BindingStores
# ---------------------------------------------------------------------------


class StubOutboundBindingStore:
    """In-memory binding store for outbound tests."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

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
# Test (a): outbound blocked → "In Progress" + dso-status:blocked label intent
# ---------------------------------------------------------------------------


def test_outbound_blocked_maps_to_in_progress(outbound_differ: ModuleType) -> None:
    """(a) outbound transition for blocked must target 'In Progress', not 'Blocked'.

    RED today: _LOCAL_TO_JIRA_STATUS maps blocked→'Blocked' which doesn't
    exist in the DIG live workflow.
    """
    ticket = _make_local_ticket(ticket_id="local-1", status="blocked")
    store = StubOutboundBindingStore()  # unbound → create mutation

    result = outbound_differ.compute_outbound_mutations(
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


def test_outbound_blocked_emits_dso_status_blocked_label(outbound_differ: ModuleType) -> None:
    """(a) outbound blocked must also emit dso-status:blocked label intent."""
    ticket = _make_local_ticket(ticket_id="local-1", status="blocked")
    store = StubOutboundBindingStore()

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
    )

    assert len(result) == 1
    m = result[0]
    label_names = {lb["label"] for lb in m.labels if lb.get("action") == "add"}
    assert "dso-status:blocked" in label_names, (
        f"outbound blocked must emit dso-status:blocked label. Got labels: {m.labels}"
    )


def test_outbound_blocked_update_maps_to_in_progress(outbound_differ: ModuleType) -> None:
    """(a) bound ticket with status=blocked → update with status='In Progress'."""
    ticket = _make_local_ticket(ticket_id="local-1", status="blocked", title="Fix me")
    store = StubOutboundBindingStore({"local-1": "DIG-100"})
    jira_snapshot = {
        "DIG-100": _make_jira_fields(status="In Progress", summary="Fix me", labels=[]),
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    # Should emit update with dso-status:blocked label (since Jira doesn't have it yet)
    assert len(result) == 1
    m = result[0]
    assert m.action == "update"
    # Status in the changed fields should be "In Progress" (not "Blocked")
    if "status" in m.fields:
        assert m.fields["status"] == "In Progress", (
            f"blocked must map to 'In Progress', got: {m.fields['status']!r}"
        )
    # dso-status:blocked label must be added
    label_adds = [lb["label"] for lb in m.labels if lb.get("action") == "add"]
    assert "dso-status:blocked" in label_adds, (
        f"Must emit dso-status:blocked label for blocked status. Got: {m.labels}"
    )


# ---------------------------------------------------------------------------
# Test (b): outbound cancelled → "Done" + dso-status:cancelled label intent
# ---------------------------------------------------------------------------


def test_outbound_cancelled_maps_to_done(outbound_differ: ModuleType) -> None:
    """(b) outbound transition for cancelled must target 'Done', not 'Cancelled'.

    RED today: _LOCAL_TO_JIRA_STATUS maps cancelled→'Cancelled' which doesn't
    exist in the DIG live workflow.
    """
    ticket = _make_local_ticket(ticket_id="local-1", status="cancelled")
    store = StubOutboundBindingStore()

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        excluded_statuses={"archived", "deleted"},  # cancelled is NOT excluded
    )

    assert len(result) == 1, f"Expected 1 create mutation, got {result}"
    m = result[0]
    assert m.action == "create"
    assert m.fields["status"] == "Done", (
        f"cancelled must map to 'Done' (live DIG workflow), got: {m.fields['status']!r}"
    )


def test_outbound_cancelled_emits_dso_status_cancelled_label(outbound_differ: ModuleType) -> None:
    """(b) outbound cancelled must also emit dso-status:cancelled label intent."""
    ticket = _make_local_ticket(ticket_id="local-1", status="cancelled")
    store = StubOutboundBindingStore()

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        excluded_statuses={"archived", "deleted"},
    )

    assert len(result) == 1
    m = result[0]
    label_names = {lb["label"] for lb in m.labels if lb.get("action") == "add"}
    assert "dso-status:cancelled" in label_names, (
        f"outbound cancelled must emit dso-status:cancelled label. Got labels: {m.labels}"
    )


# ---------------------------------------------------------------------------
# Test (c): status change blocked→in_progress removes dso-status:blocked label
# ---------------------------------------------------------------------------


def test_outbound_blocked_to_in_progress_removes_annotation_label(
    outbound_differ: ModuleType,
) -> None:
    """(c) when local status changes from blocked to in_progress, remove dso-status:blocked.

    Jira has dso-status:blocked from a previous pass; now local is in_progress.
    The differ must emit a REMOVE for dso-status:blocked.
    """
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
            labels=["dso-status:blocked"],
        ),
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    assert len(result) == 1, f"Expected 1 update mutation, got {result}"
    m = result[0]
    label_removes = [lb["label"] for lb in m.labels if lb.get("action") == "remove"]
    assert "dso-status:blocked" in label_removes, (
        f"Must remove stale dso-status:blocked when status moves to in_progress. "
        f"Got labels: {m.labels}"
    )


def test_outbound_cancelled_to_closed_removes_annotation_label(
    outbound_differ: ModuleType,
) -> None:
    """(c) variant: when closed replaces cancelled, remove dso-status:cancelled."""
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
            labels=["dso-status:cancelled"],
        ),
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    assert len(result) == 1, f"Expected 1 update mutation, got {result}"
    m = result[0]
    label_removes = [lb["label"] for lb in m.labels if lb.get("action") == "remove"]
    assert "dso-status:cancelled" in label_removes, (
        f"Must remove stale dso-status:cancelled when status moves to closed. "
        f"Got labels: {m.labels}"
    )


# ---------------------------------------------------------------------------
# Test (d): inbound dso-status:blocked label → local "blocked"
# ---------------------------------------------------------------------------


def test_inbound_dso_status_blocked_label_overrides_jira_status(
    inbound_differ: ModuleType,
) -> None:
    """(d) inbound with dso-status:blocked label → local 'blocked' regardless of workflow status.

    Jira reports "In Progress" as workflow status, but the dso-status:blocked
    label carries the lossless annotation. Local should be "blocked".
    """
    jira_snapshot = {
        "DIG-100": _make_jira_fields(
            status="In Progress",
            summary="Blocked ticket",
            labels=["dso-status:blocked"],
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

    # Local is already "blocked"; Jira says "In Progress" but has dso-status:blocked.
    # No status change should be emitted (local is correct).
    status_changes = [
        m for m in mutations
        if m.local_id == "local-1" and "status" in m.fields
    ]
    assert status_changes == [], (
        f"Inbound must NOT overwrite local 'blocked' when dso-status:blocked is present. "
        f"Got: {status_changes}"
    )


def test_inbound_dso_status_blocked_label_sets_local_blocked_when_local_is_open(
    inbound_differ: ModuleType,
) -> None:
    """(d) inbound dso-status:blocked label → local status becomes 'blocked', not 'in_progress'."""
    jira_snapshot = {
        "DIG-100": _make_jira_fields(
            status="In Progress",
            summary="Some ticket",
            labels=["dso-status:blocked"],
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

    status_mutations = [
        m for m in mutations
        if m.local_id == "local-1" and "status" in m.fields
    ]
    assert len(status_mutations) == 1, (
        f"Expected 1 status mutation. Got: {mutations}"
    )
    assert status_mutations[0].fields["status"] == "blocked", (
        f"inbound with dso-status:blocked label must set status='blocked', "
        f"got: {status_mutations[0].fields['status']!r}"
    )


def test_inbound_dso_status_cancelled_label_sets_local_cancelled(
    inbound_differ: ModuleType,
) -> None:
    """(d) inbound dso-status:cancelled label → local status = 'cancelled'."""
    jira_snapshot = {
        "DIG-200": _make_jira_fields(
            status="Done",
            summary="Cancelled ticket",
            labels=["dso-status:cancelled"],
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

    status_mutations = [
        m for m in mutations
        if m.local_id == "local-2" and "status" in m.fields
    ]
    assert len(status_mutations) == 1
    assert status_mutations[0].fields["status"] == "cancelled", (
        f"inbound with dso-status:cancelled must set status='cancelled', "
        f"got: {status_mutations[0].fields['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test (e): inbound "In Review" with no dso-status label → "in_progress"
# ---------------------------------------------------------------------------


def test_inbound_in_review_maps_to_in_progress(inbound_differ: ModuleType) -> None:
    """(e) inbound 'In Review' with no dso-status label → local 'in_progress', not 'open'.

    RED today: _JIRA_TO_LOCAL_STATUS has no entry for 'In Review', so
    _map_jira_to_local_fields returns 'open' (the dict.get default).
    """
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

    status_mutations = [
        m for m in mutations
        if m.local_id == "local-3" and "status" in m.fields
    ]
    assert len(status_mutations) == 1, (
        f"Expected 1 status mutation for 'In Review' → 'in_progress'. Got: {mutations}"
    )
    assert status_mutations[0].fields["status"] == "in_progress", (
        f"'In Review' must map to 'in_progress', got: {status_mutations[0].fields['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test (f): dso-status: labels excluded from local tag sync both directions
# ---------------------------------------------------------------------------


def test_outbound_dso_status_labels_excluded_from_tag_sync(
    outbound_differ: ModuleType,
) -> None:
    """(f) dso-status: labels must not be synced as user tags outbound.

    When local ticket has no dso-status: tags but Jira has dso-status:blocked,
    the outbound differ must NOT emit a REMOVE for that label via normal tag
    diff (it is managed by status logic only).
    """
    # Local is now in_progress (not blocked), no dso-status: tag in local tags
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
            labels=["dso-status:blocked", "user-tag"],
        ),
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    # The dso-status:blocked label removal should be via status-driven logic
    # (not via blind tag diff where it would look like "Jira has it, local doesn't")
    # Specifically: the status-driven REMOVE for dso-status:blocked should appear,
    # but a spurious tag-diff REMOVE must NOT appear as a duplicate.
    if result:
        m = result[0]
        label_removes = [lb["label"] for lb in m.labels if lb.get("action") == "remove"]
        # The remove should appear exactly once (not duplicated by tag diff)
        remove_count = label_removes.count("dso-status:blocked")
        assert remove_count <= 1, (
            f"dso-status:blocked removal should appear at most once (not duplicated). "
            f"Got {remove_count} removes."
        )


def test_inbound_dso_status_labels_excluded_from_local_tag_sync(
    inbound_differ: ModuleType,
) -> None:
    """(f) dso-status: labels must not leak into local ticket tags via inbound label sync.

    Jira has dso-status:blocked label. Local ticket has no such tag.
    The inbound label diff must NOT emit an ADD for dso-status:blocked as a
    user tag (it is a reconciler-managed annotation, not a user label).
    """
    jira_snapshot = {
        "DIG-100": _make_jira_fields(
            status="In Progress",
            summary="Blocked ticket",
            labels=["dso-status:blocked", "real-user-tag"],
        ),
    }
    store = StubInboundBindingStore({"DIG-100": "local-1"})
    local_tickets = {
        "local-1": _make_local_ticket(
            ticket_id="local-1",
            status="blocked",
            title="Blocked ticket",
            tags=[],  # no dso-status: tags locally
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
            label_adds.extend(
                lb["label"] for lb in m.labels if lb.get("action") == "add"
            )

    assert "dso-status:blocked" not in label_adds, (
        f"dso-status:blocked must NOT be added as a local user tag. "
        f"Got label adds: {label_adds}"
    )
    # real-user-tag should still come through
    assert "real-user-tag" in label_adds, (
        f"real-user-tag must still be synced inbound. Got: {label_adds}"
    )
