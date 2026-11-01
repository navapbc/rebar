"""Outbound field comparison uses the injected inbound mapper to canonicalize remote
state, then maps emitted updates back through the outbound mapper. These tests
preserve exact pre-refactor mutations and mapper injection.

Held-out oracles merged from test_canonical_outbound_equivalence_heldout: Held-out equivalence
oracle for both-empty and permissive assignee identities, plus partial baseline field-presence
semantics.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit


_REC = Path(__file__).resolve().parents[4] / "src" / "rebar" / "reconciler"


class _StubBindingStore:
    def __init__(self, l2j: dict[str, str]) -> None:
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


def _ticket(**ov) -> dict:
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


def _jira(**ov) -> dict:
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


def _emit(od, backend, t: dict, jf: dict) -> dict | None:
    """Run the canonical differ with the JiraBackend's INJECTED mappers; return the emitted
    update mutation's ``fields`` (or None if nothing emitted)."""
    bs = _StubBindingStore({"loc-1": "DIG-1"})
    muts, _ = od.compute_outbound_mutations(
        [t],
        {"DIG-1": jf},
        bs,
        outbound_mapper=backend.outbound,
        inbound_mapper=backend.inbound,
    )
    for m in muts:
        fields = getattr(m, "fields", None)
        if fields is not None:
            return dict(fields)
    return None


@pytest.fixture(scope="module")
def od() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch):
    """Pin the pre-refactor plain-wire golden independently of project rollout config."""
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", "off")

    from rebar.reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


def test_identical_emits_nothing(od, backend) -> None:
    assert _emit(od, backend, _ticket(), _jira()) is None


def test_title_change_emits_summary(od, backend) -> None:
    assert _emit(od, backend, _ticket(title="NEW"), _jira(summary="T")) == {"summary": "NEW"}


def test_description_change_emits_description(od, backend) -> None:
    assert _emit(od, backend, _ticket(description="LOCAL"), _jira(description="REMOTE")) == {
        "description": "LOCAL"
    }


def test_status_change_emits_mapped_status(od, backend) -> None:
    assert _emit(od, backend, _ticket(status="closed"), _jira(status={"name": "To Do"})) == {
        "status": "Done"
    }


def test_snapshot_canonicalized_via_injected_inbound_mapper(od) -> None:
    class SpyInbound:
        def __init__(self) -> None:
            self.seen: list[dict] = []

        def map_remote_to_local(self, remote_fields):
            self.seen.append(remote_fields)
            # Return a canonical shape equal to the local ticket so nothing is emitted.
            return {"title": "T", "description": "D", "status": "open"}

    class PassthroughOutbound:
        def map_local_to_remote(
            self,
            ticket,
            binding_store=None,
            local_ticket_types=None,
            emit_detach_clear=False,
            *,
            suppressed_out=None,
            status_map=None,
            type_map=None,
            priority_map=None,
            create_defaults=None,
        ):
            return {}

        def map_fields_to_remote(
            self,
            changed,
            ticket=None,
            binding_store=None,
            local_ticket_types=None,
            status_map=None,
            priority_map=None,
        ):
            return dict(changed)

        def resolve_assignee(self, local_value, remote_identity, *, assignee_resolver=None):
            return (local_value, False, False)

    spy = SpyInbound()
    bs = _StubBindingStore({"loc-1": "DIG-1"})
    od.compute_outbound_mutations(
        [_ticket()],
        {"DIG-1": _jira()},
        bs,
        outbound_mapper=PassthroughOutbound(),
        inbound_mapper=spy,
    )
    assert spy.seen, "the update path must canonicalize the snapshot via the injected InboundMapper"
    assert spy.seen[0].get("summary") == "T"


class _BaselineStubBindingStore(_StubBindingStore):
    """A binding store whose get_baseline returns a RAW (vendor-shaped) partial baseline,
    canonicalized at read time by the differ — exercises the absent-vs-present ancestor rule."""

    def __init__(self, l2j, baseline):
        super().__init__(l2j)
        self._baseline = baseline

    def get_baseline(self, l):  # noqa: E741
        return self._baseline


def _emit_with_baseline(od, backend, t, jf, baseline):
    bs = _BaselineStubBindingStore({"loc-1": "DIG-1"}, baseline)
    muts, _ = od.compute_outbound_mutations(
        [t], {"DIG-1": jf}, bs, outbound_mapper=backend.outbound, inbound_mapper=backend.inbound
    )
    for m in muts:
        fields = getattr(m, "fields", None)
        if fields is not None:
            return dict(fields)
    return None


def test_field_absent_from_baseline_is_no_ancestor_local_wins(od, backend) -> None:
    """A mirrored field ABSENT from the (partial) canonical baseline has no ancestor, so the
    inbound-directionality guard cannot suppress it: local-wins emits — matching today's
    whole-None-baseline arbitration (ticket 625b, canonical path)."""
    ticket = _ticket(status="closed")
    remote = _jira(status={"name": "To Do"})  # canonical remote status = "open"
    # Baseline carries only summary — NO status entry → status has no ancestor.
    emitted = _emit_with_baseline(od, backend, ticket, remote, {"summary": "T"})
    assert emitted == {"status": "Done"}


def test_field_matching_baseline_is_suppressed_for_inbound(od, backend) -> None:
    """Contrast: when the field IS present in the baseline and local matches it (unchanged
    since last sync) while remote differs, the outbound diff suppresses it (leaves the
    remote for the inbound differ) — proving the canonical baseline is genuinely consulted."""
    ticket = _ticket(status="closed")
    remote = _jira(status={"name": "To Do"})  # remote differs (canonical "open")
    # Baseline status "Done" canonicalizes to local "closed" → local unchanged since sync.
    emitted = _emit_with_baseline(od, backend, ticket, remote, {"status": {"name": "Done"}})
    assert emitted is None


# ── held-out oracles (merged from test_canonical_outbound_equivalence_heldout) ──


class _StubBindingStore_heldout:
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


def _ticket_heldout(**ov):
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


def _jira_heldout(**ov):
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


def _emit_heldout(od, backend_heldout, t, jf):
    bs = _StubBindingStore_heldout({"loc-1": "DIG-1"})
    muts, _ = od.compute_outbound_mutations(
        [t],
        {"DIG-1": jf},
        bs,
        outbound_mapper=backend_heldout.outbound,
        inbound_mapper=backend_heldout.inbound,
    )
    for m in muts:
        fields = getattr(m, "fields", None)
        if fields is not None:
            return dict(fields)
    return None


@pytest.fixture(scope="module")
def backend_heldout():
    from rebar.reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


def test_both_empty_assignee_emits_nothing(od, backend_heldout) -> None:
    """Local unassigned AND remote unassigned → no churn (golden: NO_MUTATION)."""
    assert (
        _emit_heldout(
            od, backend_heldout, _ticket_heldout(assignee=""), _jira_heldout(assignee=None)
        )
        is None
    )


def test_assignee_permissive_identity_match_emits_nothing(od, backend_heldout) -> None:
    """Local string equals a value in the canonical assignee_identity (displayName) → the
    canonical membership match suppresses the emit (golden: NO_MUTATION)."""
    jf = _jira_heldout(
        assignee={"accountId": "a1", "displayName": "Jane Doe", "emailAddress": "j@x"}
    )
    assert _emit_heldout(od, backend_heldout, _ticket_heldout(assignee="Jane Doe"), jf) is None


def test_assignee_email_match_emits_nothing(od, backend_heldout) -> None:
    """Membership match also holds against the email alias in assignee_identity."""
    jf = _jira_heldout(
        assignee={"accountId": "a1", "displayName": "Jane Doe", "emailAddress": "jane@x"}
    )
    assert _emit_heldout(od, backend_heldout, _ticket_heldout(assignee="jane@x"), jf) is None


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
