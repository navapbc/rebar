"""An inbound parent CLEAR must require POSITIVE evidence the peer once had that parent.

THE INCIDENT THIS PINS. Change 1288 (`6baa030460`) merged and the production Jira Cloud bridge, on
its next `*/20` pass, wrote 63 `EDIT {"fields":{"parent_id":""}}` events in eleven seconds --
clearing the local parent of 63 tickets across ~20 epics. Nothing was wrong with the tickets: their
parents simply never existed on the Jira side, because rebar had never pushed them.

WHY THE GUARDS DID NOT STOP IT. The clear was gated on `should_propagate_removal("parent", ...)`,
i.e. on `managed_refs`. But `add_managed_ref` is folded by the parent-set EVENT
(`reducer/_processors.py`), so a ref is "managed" the instant it is set LOCALLY -- managed never
proved the PEER ever had it. The shipped docstring said exactly that, and rested the whole
feature on one compensating guard: the same-pass suppression of an inbound field that
outbound is writing. That
guard is structurally unreachable (`39c1-2a32-b564-4b4b`): the outbound emit gate omits the parent
unless it is an `epic`, so for every non-epic parent there is no outbound parent write to suppress
against. The "residual window" the docstring called degraded-pass-only was the normal case.

WHY THIS MODULE HAS TWO HALVES, AND WHY NEITHER ALONE IS ENOUGH. This repository has repeatedly
shipped oracles that could not fail (tickets 2944, 59b2, and the vacuous `08-assign` cell). The two
states of this code that are WRONG are each satisfied by one half alone:

  * the DEFECTIVE state (clear on managed_refs alone) satisfies the positive half and FAILS the
    negative half -- it clears a parent the peer never had;
  * the REVERTED state (never clear at all) satisfies the negative half and FAILS the positive half
    -- a real de-parenting in Jira is never mirrored.

So a single assertion cannot distinguish "correct" from "one of the two failure modes". Only an
implementation that clears on POSITIVE PEER EVIDENCE and not otherwise passes both. Do not split
these tests apart, and do not delete one to make a build green.

Both halves drive the production entry point `compute_inbound_mutations` rather than a private
helper, so they survive the refactor of the layer beneath them (the reverted change had moved this
logic between modules twice) and so a missing helper reads as a failed assertion, not an
AttributeError.
"""

from __future__ import annotations

import importlib
from typing import Any

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeBindingStore:
    """The binding lookups the inbound differ uses, plus the last-synced PEER PARENT.

    `peer_parents` is the evidence channel the fix consumes: the peer parent key rebar last
    OBSERVED for a binding. Absent (the default) means "we have never observed a parent for this
    issue", which must fail safe to NO CLEAR -- that is the incident case. A version-1 store, an
    unconfirmed binding, and an out-of-window key all present as absent here, which is why absence
    has to be the safe direction rather than the interesting one.
    """

    def __init__(self, mapping: dict[str, str], peer_parents: dict[str, str] | None = None) -> None:
        self._by_key = dict(mapping)
        self._by_local = {v: k for k, v in mapping.items()}
        self._peer_parents = dict(peer_parents or {})

    def get_local_id(self, jira_key: str) -> str | None:
        return self._by_key.get(jira_key)

    def get_jira_key(self, local_id: str) -> str | None:
        return self._by_local.get(local_id)

    def get_peer_parent(self, local_id: str) -> str | None:
        return self._peer_parents.get(local_id)


class _StubMapper:
    """An `InboundMapper` that maps nothing, so only the PARENT decision is under test."""

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        return {}


def _child(parent_id: str) -> dict[str, Any]:
    """A bound child whose parent was set LOCALLY -- so the ref IS in `managed_refs`.

    This is the shape the incident ran on. `managed_refs` carrying the ref is not incidental to the
    test; it is the whole point. The defective implementation reads this as "we managed it, so its
    absence on the peer is a deletion", and that inference is what destroyed 63 edges.
    """
    return {
        "ticket_id": "local-c",
        "parent_id": parent_id,
        "managed_refs": [["parent", parent_id]],
        "deps": [],
    }


def _parent_field_of(mutations: list[Any]) -> tuple[bool, Any]:
    """Return (emitted, value) for the `parent_id` field across all inbound mutations."""
    for m in mutations:
        fields = getattr(m, "fields", None) or {}
        if "parent_id" in fields:
            return True, fields["parent_id"]
    return False, None


def _inbound(snapshot: dict[str, dict[str, Any]], bindings: _FakeBindingStore) -> list[Any]:
    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")
    mutations, _suppressed = inbound_differ.compute_inbound_mutations(
        snapshot,
        bindings,
        {"local-c": _child("local-p"), "local-p": {"ticket_id": "local-p", "deps": []}},
        # No outbound context: the incident pass had no parent write to suppress against.
        None,
        inbound_mapper=_StubMapper(),
    )
    return list(mutations)


# ---------------------------------------------------------------------------
# NEGATIVE half -- the incident. RED against the defective implementation.
# ---------------------------------------------------------------------------


def test_a_parent_the_peer_never_had_is_not_cleared() -> None:
    """THE INCIDENT. Local parent set locally, never pushed; Jira OBSERVED to have no parent.

    There is no evidence the peer ever carried this parent, so its absence proves nothing and the
    local edge must be left alone. The defective implementation emits `parent_id: ""` here, which is
    precisely the write that orphaned 63 tickets in production.

    `outbound` is deliberately None: the incident pass had no outbound parent write, because the
    outbound emit gate never emits one for a non-epic parent (39c1). Passing an outbound context
    here would hide the defect behind a guard that cannot fire in production.
    """
    bindings = _FakeBindingStore({"DC-1": "local-c", "DC-9": "local-p"})  # no peer_parents: unknown
    snapshot = {"DC-1": {"parent": None}}  # OBSERVED, and the peer has no parent

    emitted, value = _parent_field_of(_inbound(snapshot, bindings))

    assert not emitted, (
        "REGRESSION -- the 63-ticket incident. A local parent rebar never pushed was cleared "
        f"because it is in managed_refs (emitted parent_id={value!r}). managed_refs records "
        "that we "
        "created the ref LOCALLY, never that the peer had it, so the peer's silence is not a "
        "deletion. A clear requires POSITIVE evidence the peer once carried this parent."
    )


# ---------------------------------------------------------------------------
# POSITIVE half -- the feature. RED against the reverted implementation.
# ---------------------------------------------------------------------------


def test_a_parent_the_peer_once_had_is_cleared() -> None:
    """The feature the incident was trying to deliver, with the evidence it was missing.

    rebar last OBSERVED this issue carrying parent `DC-9`, and this pass observes it carrying none.
    That is a real de-parenting on the peer and MUST be mirrored -- otherwise the reverted
    "never clear" behaviour stands and a human's de-parenting in Jira is invisible forever.

    Without this half the negative half alone is satisfied by deleting the feature, which is exactly
    the state this module exists to move off of.
    """
    bindings = _FakeBindingStore(
        {"DC-1": "local-c", "DC-9": "local-p"}, peer_parents={"local-c": "DC-9"}
    )
    snapshot = {"DC-1": {"parent": None}}  # OBSERVED, and the peer's parent is now GONE

    emitted, value = _parent_field_of(_inbound(snapshot, bindings))

    assert emitted and not value, (
        "the peer's last-synced parent was DC-9 and the peer now has NONE -- a real de-parenting, "
        f"which must be mirrored locally (emitted={emitted}, value={value!r}). If this fails while "
        "the negative case passes, the clear has been removed rather than made evidence-based."
    )


def test_an_unobserved_parent_is_never_cleared_even_with_peer_evidence() -> None:
    """The fail-open read must still fail SAFE: no `parent` key means the read told us nothing.

    Peer evidence exists, so the only thing standing between this pass and a clear is the
    observation itself. `get_parent_map` degrades to `{}` on any REST failure and a truncated page
    walk simply omits issues, so an ABSENT key must never be read as "the peer has no parent" --
    this epic already fixed three silent-truncation defects in that path.
    """
    bindings = _FakeBindingStore(
        {"DC-1": "local-c", "DC-9": "local-p"}, peer_parents={"local-c": "DC-9"}
    )
    snapshot = {"DC-1": {}}  # the parent key is ABSENT: unobserved, not "no parent"

    emitted, value = _parent_field_of(_inbound(snapshot, bindings))

    assert not emitted, (
        "an UNOBSERVED parent was treated as an observed absence and cleared the local edge "
        f"(emitted parent_id={value!r}). Absence of data is not evidence of absence of a parent."
    )
