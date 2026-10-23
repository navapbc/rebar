"""Inbound parent clears require both an observed parentless peer and evidence that Jira
previously held the parent. Locally managed refs alone do not prove peer ownership; both
no-clear and real-clear controls are required."""

from __future__ import annotations

import importlib
from typing import Any

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeBindingStore:
    """Expose the last parent observed on Jira; missing evidence must prevent a clear."""

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
    """Give the child a locally managed parent without implying Jira ever held it."""
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
    inbound_differ = importlib.import_module("rebar.reconciler.inbound_differ")
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
    """A local parent never observed on Jira must survive a parentless Jira snapshot."""
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
    """A parent previously observed on Jira must clear when Jira now reports none."""
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
    """An omitted parent field is not an observed clear, even with prior peer evidence."""
    bindings = _FakeBindingStore(
        {"DC-1": "local-c", "DC-9": "local-p"}, peer_parents={"local-c": "DC-9"}
    )
    snapshot = {"DC-1": {}}  # the parent key is ABSENT: unobserved, not "no parent"

    emitted, value = _parent_field_of(_inbound(snapshot, bindings))

    assert not emitted, (
        "an UNOBSERVED parent was treated as an observed absence and cleared the local edge "
        f"(emitted parent_id={value!r}). Absence of data is not evidence of absence of a parent."
    )
