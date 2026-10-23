"""Held-out equivalence oracle for both-empty and permissive assignee identities, plus
partial baseline field-presence semantics."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_REC = Path(__file__).resolve().parents[4] / "src" / "rebar" / "reconciler"


class _StubBindingStore:
    def __init__(self, l2j):
        self._l2j = l2j
        self._j2l = {v: k for k, v in l2j.items()}

    def get_jira_key(self, l):  # noqa: E741
        return self._l2j.get(l)

    def is_bound(self, l):  # noqa: E741
        return l in self._l2j

    def get_local_id(self, j):
        return self._j2l.get(j)

    def is_pending(self, l):  # noqa: E741
        return False

    def get_baseline(self, l):  # noqa: E741
        return None


def _ticket(**ov):
    t = {
        "ticket_id": "loc-1",
        "title": "T",
        "description": "D",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
    }
    t.update(ov)
    return t


def _jira(**ov):
    f = {
        "summary": "T",
        "description": "D",
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": None,
        "labels": [],
    }
    f.update(ov)
    return f


def _emit(od, backend, t, jf):
    bs = _StubBindingStore({"loc-1": "DIG-1"})
    muts, _ = od.compute_outbound_mutations(
        [t], {"DIG-1": jf}, bs, outbound_mapper=backend.outbound, inbound_mapper=backend.inbound
    )
    for m in muts:
        fields = getattr(m, "fields", None)
        if fields is not None:
            return dict(fields)
    return None


@pytest.fixture(scope="module")
def od() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


@pytest.fixture(scope="module")
def backend():
    from rebar.reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


def test_both_empty_assignee_emits_nothing(od, backend) -> None:
    """Local unassigned AND remote unassigned → no churn (golden: NO_MUTATION)."""
    assert _emit(od, backend, _ticket(assignee=""), _jira(assignee=None)) is None


def test_assignee_permissive_identity_match_emits_nothing(od, backend) -> None:
    """Local string equals a value in the canonical assignee_identity (displayName) → the
    canonical membership match suppresses the emit (golden: NO_MUTATION)."""
    jf = _jira(assignee={"accountId": "a1", "displayName": "Jane Doe", "emailAddress": "j@x"})
    assert _emit(od, backend, _ticket(assignee="Jane Doe"), jf) is None


def test_assignee_email_match_emits_nothing(od, backend) -> None:
    """Membership match also holds against the email alias in assignee_identity."""
    jf = _jira(assignee={"accountId": "a1", "displayName": "Jane Doe", "emailAddress": "jane@x"})
    assert _emit(od, backend, _ticket(assignee="jane@x"), jf) is None


# ── partial-baseline map_remote_to_local: presence semantics ────────────────
def test_map_remote_to_local_partial_omits_absent_keys() -> None:
    """A partial `_BASELINE_FIELDS` subset maps ONLY the keys present; an absent vendor key
    yields NO corresponding canonical key (never a default). A FULL snapshot is unchanged."""
    from rebar.reconciler.adapters.jira.backend import JiraBackend

    inbound = JiraBackend(transport=object()).inbound

    # Partial input: only status present.
    partial = inbound.map_remote_to_local({"status": {"name": "To Do"}})
    # Absent vendor keys must NOT be defaulted into canonical output.
    assert "title" not in partial and "description" not in partial
    assert "assignee_identity" not in partial  # assignee absent -> no identity emitted

    # Full snapshot: assignee present -> assignee_identity emitted.
    full = inbound.map_remote_to_local(
        {
            "summary": "T",
            "description": "D",
            "status": {"name": "To Do"},
            "assignee": {"accountId": "a1", "displayName": "J", "emailAddress": "j@x"},
        }
    )
    assert full["assignee_identity"]["account_id"] == "a1"
