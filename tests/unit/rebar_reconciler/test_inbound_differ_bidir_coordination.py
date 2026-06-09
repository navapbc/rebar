"""Bug 3bf8: inbound differ must suppress mutations that contradict outbound.

In a single bidirectional pass, outbound and inbound differs both run
against the same pre-pass snapshot. When the local side has just added a
label (or changed a scalar field), outbound correctly emits ADD-X but
without coordination the inbound differ emits REMOVE-X (since the
snapshot shows Jira lacking X). The apply pass then runs both and the
inbound EDIT clobbers the local-side change.

Fix: ``compute_inbound_mutations`` accepts an optional
``outbound_mutations`` parameter and filters its emissions so that:
  - label ADD is suppressed when outbound is REMOVING the same label
    for the same jira_key
  - label REMOVE is suppressed when outbound is ADDING the same label
    for the same jira_key
  - scalar field updates are suppressed when outbound is updating the
    same field on the same jira_key

The local-side change wins for the pass; the next pass converges both
sides without a phase-2 snapshot refresh.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_differ.py"
)
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
def inbound_differ() -> ModuleType:
    return _load_module("inbound_differ_bidir", INBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ_bidir", OUTBOUND_DIFFER_PATH)


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        # bindings: {jira_key: local_id}
        self._bindings: dict[str, str] = bindings or {}

    def get_local_id(self, jira_key: str) -> str | None:
        return self._bindings.get(jira_key)


def _baseline_local(label_list: list[str]) -> dict:
    return {
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "alice",
        "tags": label_list,
    }


def _baseline_jira(label_list: list[str]) -> dict:
    return {
        "summary": "T",
        "description": "D",
        "issuetype": "Task",
        "priority": "Medium",
        "status": "To Do",
        "assignee": "alice",
        "labels": label_list,
    }


# ---------------------------------------------------------------------------
# Label coordination tests
# ---------------------------------------------------------------------------


def test_inbound_remove_suppressed_when_outbound_adds_same_label(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Local just added 'ob-added'; outbound emits ADD; inbound must NOT emit REMOVE."""
    jira_snapshot = {"PROJ-1": _baseline_jira(["labelprobe"])}
    local_tickets = {"local-1": _baseline_local(["labelprobe", "ob-added"])}
    store = StubBindingStore({"PROJ-1": "local-1"})

    outbound_mutations = [
        outbound_differ.OutboundMutation(
            local_id="local-1",
            jira_key="PROJ-1",
            action="update",
            fields={},
            labels=[{"action": "add", "label": "ob-added"}],
        )
    ]

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
        outbound_mutations=outbound_mutations,
    )

    # Inbound must not emit a REMOVE for 'ob-added'.
    for m in result:
        for lm in m.labels:
            assert not (
                lm.get("action") == "remove" and lm.get("label") == "ob-added"
            ), f"Inbound emitted contradictory REMOVE ob-added: {m}"
    # One label REMOVE was suppressed.
    assert suppressed == 1


def test_inbound_add_suppressed_when_outbound_removes_same_label(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Local just removed 'ib-add'; outbound emits REMOVE; inbound must NOT emit ADD."""
    # Local lacks 'ib-add'; Jira still has it (local-side change is fresher).
    jira_snapshot = {"PROJ-2": _baseline_jira(["labelprobe", "ib-add"])}
    local_tickets = {"local-2": _baseline_local(["labelprobe"])}
    store = StubBindingStore({"PROJ-2": "local-2"})

    outbound_mutations = [
        outbound_differ.OutboundMutation(
            local_id="local-2",
            jira_key="PROJ-2",
            action="update",
            fields={},
            labels=[{"action": "remove", "label": "ib-add"}],
        )
    ]

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
        outbound_mutations=outbound_mutations,
    )

    for m in result:
        for lm in m.labels:
            assert not (lm.get("action") == "add" and lm.get("label") == "ib-add"), (
                f"Inbound emitted contradictory ADD ib-add: {m}"
            )
    # One label ADD was suppressed.
    assert suppressed == 1


def test_inbound_scalar_field_suppressed_when_outbound_updates_same_field(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Local just changed description; outbound emits update for it; inbound must NOT echo back the old value."""
    jira_snapshot = {
        "PROJ-3": {
            "summary": "T",
            "description": "old jira desc",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
        }
    }
    local_tickets = {
        "local-3": {
            "title": "T",
            "description": "new local desc",
            "ticket_type": "task",
            "priority": 2,
            "status": "open",
            "assignee": "alice",
            "tags": [],
        }
    }
    store = StubBindingStore({"PROJ-3": "local-3"})

    outbound_mutations = [
        outbound_differ.OutboundMutation(
            local_id="local-3",
            jira_key="PROJ-3",
            action="update",
            fields={"description": "new local desc"},
            labels=[],
        )
    ]

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
        outbound_mutations=outbound_mutations,
    )

    for m in result:
        assert "description" not in m.fields, (
            f"Inbound emitted contradictory description update: {m}"
        )
    # One scalar field update was suppressed.
    assert suppressed == 1


# ---------------------------------------------------------------------------
# Backward-compat: existing callers (no outbound_mutations) still work.
# ---------------------------------------------------------------------------


def test_default_outbound_mutations_param_preserves_legacy_behavior(
    inbound_differ: ModuleType,
) -> None:
    """Without outbound_mutations, behavior matches pre-fix semantics."""
    jira_snapshot = {"PROJ-9": _baseline_jira(["only-on-jira"])}
    local_tickets = {"local-9": _baseline_local([])}
    store = StubBindingStore({"PROJ-9": "local-9"})

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    assert len(result) == 1
    # Without outbound_mutations, no suppression occurs.
    assert suppressed == 0
    add_labels = [lm for lm in result[0].labels if lm.get("action") == "add"]
    assert any(lm.get("label") == "only-on-jira" for lm in add_labels)


# ---------------------------------------------------------------------------
# _build_outbound_context direct tests (cycle-1 review finding 2).
# ---------------------------------------------------------------------------


def _ob_mut(
    outbound_differ: ModuleType,
    *,
    jira_key: str,
    fields: dict | None = None,
    labels: list | None = None,
) -> object:
    return outbound_differ.OutboundMutation(
        local_id="local-x",
        jira_key=jira_key,
        action="update",
        fields=fields or {},
        labels=labels or [],
    )


def test_build_outbound_context_empty_returns_empty_index(
    inbound_differ: ModuleType,
) -> None:
    """Empty / None outbound list -> empty index."""
    assert inbound_differ._build_outbound_context(None) == {}
    assert inbound_differ._build_outbound_context([]) == {}


def test_build_outbound_context_label_adds_only(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Label adds populate label_adds; label_removes stays empty."""
    om = _ob_mut(
        outbound_differ,
        jira_key="PROJ-1",
        labels=[
            {"action": "add", "label": "alpha"},
            {"action": "add", "label": "beta"},
        ],
    )
    ctx = inbound_differ._build_outbound_context([om])
    assert "PROJ-1" in ctx
    entry = ctx["PROJ-1"]
    assert entry["label_adds"] == {"alpha", "beta"}
    assert entry["label_removes"] == set()
    assert entry["fields"] == set()


def test_build_outbound_context_mixed_adds_and_removes_same_jira_key(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Mixed add/remove on same jira_key populates both sets correctly."""
    om = _ob_mut(
        outbound_differ,
        jira_key="PROJ-2",
        labels=[
            {"action": "add", "label": "keep-me"},
            {"action": "remove", "label": "drop-me"},
            {"action": "add", "label": "another-add"},
        ],
    )
    ctx = inbound_differ._build_outbound_context([om])
    entry = ctx["PROJ-2"]
    assert entry["label_adds"] == {"keep-me", "another-add"}
    assert entry["label_removes"] == {"drop-me"}


def test_build_outbound_context_field_updates_populate_fields_set(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Outbound with field updates -> fields set populated with field names."""
    om = _ob_mut(
        outbound_differ,
        jira_key="PROJ-3",
        fields={"description": "new", "priority": 1},
    )
    ctx = inbound_differ._build_outbound_context([om])
    entry = ctx["PROJ-3"]
    assert entry["fields"] == {"description", "priority"}
    assert entry["label_adds"] == set()
    assert entry["label_removes"] == set()


# ---------------------------------------------------------------------------
# Cycle-2 finding 2: malformed label-entry edge cases for
# _build_outbound_context (must skip gracefully, never raise).
# ---------------------------------------------------------------------------


def test_build_outbound_context_skips_label_entry_missing_action(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Label entry without an 'action' key is ignored — neither set populated."""
    om = _ob_mut(
        outbound_differ,
        jira_key="PROJ-MA",
        labels=[{"label": "orphan-no-action"}],
    )
    ctx = inbound_differ._build_outbound_context([om])
    entry = ctx["PROJ-MA"]
    assert entry["label_adds"] == set()
    assert entry["label_removes"] == set()


def test_build_outbound_context_skips_label_entry_missing_label(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Label entry without a 'label' key is skipped (current guard: `if not label: continue`)."""
    om = _ob_mut(
        outbound_differ,
        jira_key="PROJ-ML",
        labels=[{"action": "add"}, {"action": "remove"}],
    )
    ctx = inbound_differ._build_outbound_context([om])
    entry = ctx["PROJ-ML"]
    assert entry["label_adds"] == set()
    assert entry["label_removes"] == set()


def test_build_outbound_context_mixed_valid_and_malformed_entries(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Valid entries index correctly; malformed entries are skipped without raising."""
    om = _ob_mut(
        outbound_differ,
        jira_key="PROJ-MIX",
        labels=[
            {"action": "add", "label": "good-add"},
            {"label": "no-action"},  # malformed: missing action
            {"action": "remove"},  # malformed: missing label
            {"action": "remove", "label": "good-remove"},
            {},  # entirely empty
            "not-a-dict",  # not a dict at all
        ],
    )
    ctx = inbound_differ._build_outbound_context([om])
    entry = ctx["PROJ-MIX"]
    assert entry["label_adds"] == {"good-add"}
    assert entry["label_removes"] == {"good-remove"}


# ---------------------------------------------------------------------------
# Cycle-2 finding 1: combined field + label suppression on same jira_key —
# verify suppression_count sums across mutation types.
# ---------------------------------------------------------------------------


def test_combined_field_and_label_suppression_same_jira_key(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """When outbound is updating both a scalar field AND adding+removing labels
    on the same jira_key, suppression_count must equal the sum of every
    inbound mutation type suppressed (field + label-add + label-remove).
    """
    # Local just: changed description, added 'ob-added', removed 'ib-add'.
    # Jira snapshot: old description, lacks 'ob-added', still has 'ib-add'.
    jira_snapshot = {
        "PROJ-C": {
            "summary": "T",
            "description": "old jira desc",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": ["keep", "ib-add"],
        }
    }
    local_tickets = {
        "local-c": {
            "title": "T",
            "description": "new local desc",
            "ticket_type": "task",
            "priority": 2,
            "status": "open",
            "assignee": "alice",
            "tags": ["keep", "ob-added"],
        }
    }
    store = StubBindingStore({"PROJ-C": "local-c"})

    outbound_mutations = [
        outbound_differ.OutboundMutation(
            local_id="local-c",
            jira_key="PROJ-C",
            action="update",
            fields={"description": "new local desc"},
            labels=[
                {"action": "add", "label": "ob-added"},
                {"action": "remove", "label": "ib-add"},
            ],
        )
    ]

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
        outbound_mutations=outbound_mutations,
    )

    # No contradictory inbound emissions survive.
    for m in result:
        assert "description" not in m.fields, (
            f"Inbound emitted contradictory description update: {m}"
        )
        for lm in m.labels:
            assert not (
                lm.get("action") == "remove" and lm.get("label") == "ob-added"
            ), f"Inbound emitted contradictory REMOVE ob-added: {m}"
            assert not (lm.get("action") == "add" and lm.get("label") == "ib-add"), (
                f"Inbound emitted contradictory ADD ib-add: {m}"
            )

    # Three distinct suppressions on the same jira_key:
    #   1 scalar field (description) + 1 label REMOVE (ob-added) + 1 label ADD (ib-add).
    assert suppressed == 3, (
        f"Expected 3 suppressions (field + label-add + label-remove), got {suppressed}"
    )


# ---------------------------------------------------------------------------
# Bug 8b25: outbound emits the parent under the Jira field name ``parent``;
# inbound emits the same logical change under the LOCAL field name
# ``parent_id``. Bidirectional suppression keys on inbound field names, so
# without canonicalisation the inbound ``parent_id`` re-emission is NEVER
# suppressed by an outbound ``parent`` reparent. The two differs then
# oscillate every pass against the stale pre-pass Jira snapshot — the
# perpetual ``fields=['parent']`` churn and the e2e probe's parent FAIL.
# Live-traced and proven in the minimal-repro loop (DIG-5446 flip-flopping
# DIG-5445 <-> DIG-5447 across passes until this fix converged it to 0).
# ---------------------------------------------------------------------------


def test_build_outbound_context_canonicalizes_parent_to_parent_id(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Outbound ``parent`` field is recorded under the inbound name ``parent_id``.

    The outbound differ writes ``fields["parent"] = <jira_key>``; the
    suppression set must store ``parent_id`` so the inbound scalar filter
    (which keys on local field names) matches.
    """
    om = _ob_mut(
        outbound_differ,
        jira_key="PROJ-P",
        fields={"parent": "DIG-9999"},
    )
    ctx = inbound_differ._build_outbound_context([om])
    entry = ctx["PROJ-P"]
    assert entry["fields"] == {"parent_id"}, (
        "outbound 'parent' must be canonicalised to inbound 'parent_id' so "
        f"bidirectional suppression matches; got {entry['fields']!r}"
    )


def test_inbound_parent_id_suppressed_when_outbound_reparents(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """Local just reparented to E2; outbound emits parent->DIG-E2; inbound must
    NOT echo back the stale Jira parent_id (the old epic) from the pre-pass
    snapshot. Before bug-8b25's fix this suppression silently failed because
    outbound recorded 'parent' but inbound emitted 'parent_id'.
    """
    # Jira snapshot still shows the OLD parent (DIG-OLD -> local epic-old);
    # local has been reparented to epic-new. Local-side change is fresher.
    jira_snapshot = {
        "PROJ-CHILD": {
            "summary": "T",
            "description": "D",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            "parent": {"key": "DIG-OLD"},
        }
    }
    local_tickets = {
        "local-child": {
            "title": "T",
            "description": "D",
            "ticket_type": "task",
            "priority": 2,
            "status": "open",
            "assignee": "alice",
            "tags": [],
            "parent_id": "epic-new",
        }
    }
    # binding_store resolves the stale Jira parent key to its local id so the
    # inbound differ would (absent suppression) emit parent_id=epic-old.
    store = StubBindingStore(
        {"PROJ-CHILD": "local-child", "DIG-OLD": "epic-old", "DIG-NEW": "epic-new"}
    )

    outbound_mutations = [
        outbound_differ.OutboundMutation(
            local_id="local-child",
            jira_key="PROJ-CHILD",
            action="update",
            # The outbound differ resolves epic-new -> DIG-NEW and writes it
            # under the Jira field name 'parent'.
            fields={"parent": "DIG-NEW"},
            labels=[],
        )
    ]

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
        outbound_mutations=outbound_mutations,
    )

    for m in result:
        assert "parent_id" not in m.fields, (
            "Inbound emitted contradictory parent_id update that would clobber "
            f"the just-reparented local value and re-trigger oscillation: {m}"
        )
    # Exactly one scalar suppression (the parent_id echo).
    assert suppressed == 1, (
        f"Expected the inbound parent_id echo to be suppressed; got {suppressed}"
    )
