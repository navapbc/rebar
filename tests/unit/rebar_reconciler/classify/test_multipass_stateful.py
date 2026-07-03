"""Multi-pass stateful + fault-injection convergence (epic 3006-e198 foundation).

This is the epic's named "faithful-persistence" cell — the gap that HID drift
class B. A model world is driven through N passes where the per-binding baseline
is PERSISTED to a REAL temp bindings.json between passes (not an in-memory proxy).
A partial-apply failure is injected mid-pass (a decision's apply raises); the NEXT
pass must re-derive the SAME decision from the persisted state and self-heal to the
fixed point.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from ._load import load_classify

c = load_classify()
ObservedJira = c.ObservedJira
DecisionKind = c.DecisionKind
JiraObservation = c.JiraObservation

_BS_SRC = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "binding_store.py"
)
_spec = importlib.util.spec_from_file_location("binding_store_multipass", _BS_SRC)
assert _spec and _spec.loader
_bs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _bs
_spec.loader.exec_module(_bs)
BindingStore = _bs.BindingStore


class InjectedApplyFailure(RuntimeError):
    pass


def _classify_pair(store, jira_key, local, snapshot):
    """Classify one bound pair the way the live consumer does, reading the
    PERSISTED binding entry (incl. baseline) from ``store``."""
    local_id = store.get_local_id(jira_key)
    entry = store.all_bindings().get(local_id)
    if jira_key in snapshot:
        obs = JiraObservation(ObservedJira.PRESENT, key=jira_key, fields=snapshot[jira_key])
    else:
        obs = JiraObservation(ObservedJira.ABSENT_IN_WINDOW, key=jira_key)
    return c.classify(local, obs, entry, store.get_baseline(local_id))


def test_baseline_persisted_between_passes_and_self_heals_after_partial_failure(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    store = BindingStore(tracker)

    # Two bound pairs: one active (should SYNC + advance baseline), one archived
    # (should TERMINAL_TRANSITION). Local ground truth.
    store.bind_confirm("loc-active", "REB-1")
    store.bind_confirm("loc-arch", "REB-2")
    store.save()

    locals_ = {
        "REB-1": {"ticket_id": "loc-active", "status": "in_progress", "archived": False},
        "REB-2": {"ticket_id": "loc-arch", "status": "archived", "archived": True},
    }
    # Jira ground truth: both present; REB-2 still "To Do" until transitioned.
    jira_status = {"REB-1": "In Progress", "REB-2": "To Do"}

    def snapshot():
        return {
            k: {"summary": k, "status": v, "priority": "Low", "description": k, "assignee": ""}
            for k, v in jira_status.items()
        }

    # -- Pass 1: inject a partial-apply failure on the TERMINAL_TRANSITION -----
    # The SYNC pair advances its baseline and persists; the terminal transition
    # raises mid-apply BEFORE Jira flips → baseline for REB-2 is NOT advanced and
    # Jira stays "To Do".
    applied_pass1 = []
    for jira_key in ("REB-1", "REB-2"):
        d = _classify_pair(store, jira_key, locals_[jira_key], snapshot())
        try:
            if d.kind is DecisionKind.SYNC_FIELDS:
                # advance baseline (echo-suppression bookkeeping) + persist
                store.set_baseline(store.get_local_id(jira_key), snapshot()[jira_key])
                store.save()
                applied_pass1.append((jira_key, d.kind))
            elif d.kind is DecisionKind.TERMINAL_TRANSITION:
                raise InjectedApplyFailure("Jira transition failed mid-pass")
        except InjectedApplyFailure:
            applied_pass1.append((jira_key, "FAILED"))

    assert ("REB-1", DecisionKind.SYNC_FIELDS) in applied_pass1
    assert ("REB-2", "FAILED") in applied_pass1
    # The active baseline persisted; the failed terminal transition did NOT flip Jira.
    reloaded = BindingStore(tracker)
    assert reloaded.get_baseline("loc-active") is not None
    assert jira_status["REB-2"] == "To Do"  # unhealed

    # -- Pass 2: re-derive from persisted state → SAME terminal decision, heal --
    store2 = BindingStore(tracker)
    d2 = _classify_pair(store2, "REB-2", locals_["REB-2"], snapshot())
    assert d2.kind is DecisionKind.TERMINAL_TRANSITION  # self-heal: re-fires
    jira_status["REB-2"] = "Done"  # this time the apply succeeds
    store2.set_baseline("loc-arch", snapshot()["REB-2"])
    store2.save()

    # -- Pass 3: fixed point — archived local + Jira Done → NOOP, no oscillation -
    store3 = BindingStore(tracker)
    for _ in range(3):
        d_active = _classify_pair(store3, "REB-1", locals_["REB-1"], snapshot())
        d_arch = _classify_pair(store3, "REB-2", locals_["REB-2"], snapshot())
        assert d_active.kind is DecisionKind.SYNC_FIELDS
        assert not d_active.is_acting
        assert d_arch.kind is DecisionKind.NOOP  # terminal + Jira Done = steady state
        assert not d_arch.is_acting
