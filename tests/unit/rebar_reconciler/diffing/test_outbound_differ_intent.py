"""Unit tests for outbound_differ label-intent gating (bug a06c).

Approach 1: outbound differ only emits REMOVE for a label when the
caller-supplied ``local_label_intent`` map confirms the label was at
some point in local's tag set (i.e., local user actually had it and
removed it). When the label was never in local's history, the REMOVE
is suppressed — the label was added on the Jira side and the inbound
differ will (correctly) propagate it locally.

This file is RED before the outbound_differ change lands.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
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
    return _load_module("outbound_differ_intent", OUTBOUND_DIFFER_PATH)


class StubBindingStore:
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


def _make_ticket(ticket_id: str, tags: list[str]) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": "T",
        "description": "D",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "",
        "tags": tags,
        "comments": [],
        "deps": [],
    }


def _jira(labels: list[str]) -> dict:
    return {
        "summary": "T",
        "description": "D",
        "issuetype": "Task",
        "priority": "Medium",
        "status": "To Do",
        "assignee": "",
        "labels": labels,
    }


def test_a06c_jira_only_label_not_in_intent_suppresses_remove(
    outbound_differ: ModuleType,
) -> None:
    """Bug a06c: Jira added X side-band, local never had X -> NO outbound REMOVE.

    Without intent gating: outbound emits REMOVE X (spurious) which
    triggers PR #457 bidir suppression to cancel the inbound ADD X,
    silently dropping the label. With intent gating: REMOVE X is
    suppressed at source -> inbound ADD X applies normally.
    """
    ticket = _make_ticket("L1", tags=["labelprobe"])
    store = StubBindingStore({"L1": "PROJ-1"})
    snap = {"PROJ-1": _jira(["labelprobe", "ib-added"])}
    intent_map = {"L1": {"labelprobe"}}  # local never had ib-added

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snap,
        binding_store=store,
        config=outbound_differ.OutboundDiffConfig(
            local_label_intent=intent_map,
        ),
    )

    # The diff would normally produce an "update" with REMOVE ib-added.
    # With intent gating it produces no mutation (or an update with no
    # label-remove for ib-added).
    label_removes = [lb["label"] for m in result for lb in m.labels if lb.get("action") == "remove"]
    assert "ib-added" not in label_removes


def test_legitimate_removal_in_intent_emits_remove(
    outbound_differ: ModuleType,
) -> None:
    """Local genuinely had X then removed it -> outbound MUST emit REMOVE X.

    This is the local-wins guarantee for label deletion. If we suppressed
    this REMOVE, local label deletion would be lost (the symptom the
    decider used to reject Approach 2).
    """
    ticket = _make_ticket("L1", tags=["labelprobe"])
    store = StubBindingStore({"L1": "PROJ-1"})
    snap = {"PROJ-1": _jira(["labelprobe", "to-remove"])}
    # Intent set includes "to-remove" — meaning local had it at some
    # point post-bind, which proves the user explicitly removed it.
    intent_map = {"L1": {"labelprobe", "to-remove"}}

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snap,
        binding_store=store,
        config=outbound_differ.OutboundDiffConfig(
            local_label_intent=intent_map,
        ),
    )

    label_removes = [lb["label"] for m in result for lb in m.labels if lb.get("action") == "remove"]
    assert "to-remove" in label_removes


def test_intent_none_preserves_legacy_behavior(
    outbound_differ: ModuleType,
) -> None:
    """When local_label_intent is None, behavior is unchanged from pre-fix.

    This guards every existing test in test_outbound_differ.py — they
    do not pass local_label_intent and must continue to emit REMOVE
    for any Jira-only label.
    """
    ticket = _make_ticket("L1", tags=["shared"])
    store = StubBindingStore({"L1": "PROJ-1"})
    snap = {"PROJ-1": _jira(["jira-only", "shared"])}

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snap,
        binding_store=store,
        # local_label_intent omitted (legacy callers)
    )

    label_removes = [lb["label"] for m in result for lb in m.labels if lb.get("action") == "remove"]
    assert "jira-only" in label_removes


def test_missing_ticket_in_intent_map_skips_removes(
    outbound_differ: ModuleType,
) -> None:
    """Lazy first-pass safety: intent map provided but missing this
    ticket's entry -> treat as no intent, suppress all REMOVEs.

    Failure mode is safe (no data loss; Jira label staleness only),
    matching the decider's specified degradation path.
    """
    ticket = _make_ticket("L1", tags=["labelprobe"])
    store = StubBindingStore({"L1": "PROJ-1"})
    snap = {"PROJ-1": _jira(["labelprobe", "anything"])}
    intent_map: dict[str, set[str]] = {}  # L1 not present

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snap,
        binding_store=store,
        config=outbound_differ.OutboundDiffConfig(
            local_label_intent=intent_map,
        ),
    )

    label_removes = [lb["label"] for m in result for lb in m.labels if lb.get("action") == "remove"]
    assert label_removes == []


def test_user_added_label_in_intent_emits_add(
    outbound_differ: ModuleType,
) -> None:
    """User-added (intent-tracked) local-only labels DO propagate outbound."""
    ticket = _make_ticket("L1", tags=["local-only", "shared"])
    store = StubBindingStore({"L1": "PROJ-1"})
    snap = {"PROJ-1": _jira(["shared"])}
    intent_map = {"L1": {"local-only", "shared"}}

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snap,
        binding_store=store,
        config=outbound_differ.OutboundDiffConfig(
            local_label_intent=intent_map,
        ),
    )

    label_adds = [lb["label"] for m in result for lb in m.labels if lb.get("action") == "add"]
    assert "local-only" in label_adds


def test_inbound_applied_label_not_in_intent_suppresses_add(
    outbound_differ: ModuleType,
) -> None:
    """T4 IB-REMOVE preservation: a label that lives in local's current
    tag set ONLY because the inbound differ applied it (not in intent
    set because the applier marked the EDIT as source=inbound) must NOT
    produce an outbound ADD.

    Without this gate, the cycle is: T3 inbound-add X -> local has X but
    intent excludes X -> later T4 Jira-removes X -> inbound emits REMOVE
    X, outbound emits ADD X, PR #457 suppression picks outbound -> X
    is re-added on Jira and stays local. Local can never delete an
    inbound-acknowledged label via Jira side. Asymmetry breaks D.
    """
    ticket = _make_ticket("L1", tags=["labelprobe", "ib-added"])
    store = StubBindingStore({"L1": "PROJ-1"})
    snap = {"PROJ-1": _jira(["labelprobe"])}  # jira just removed ib-added
    # Intent excludes ib-added because the original add was inbound.
    intent_map = {"L1": {"labelprobe"}}

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snap,
        binding_store=store,
        config=outbound_differ.OutboundDiffConfig(
            local_label_intent=intent_map,
        ),
    )

    label_adds = [lb["label"] for m in result for lb in m.labels if lb.get("action") == "add"]
    assert "ib-added" not in label_adds
