"""Exercise real outbound and inbound differs together when Jira removes a parent. Outbound must
not restore an observed peer removal, but must propagate a later local re-parent."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE = REPO_ROOT / "src" / "rebar" / "reconciler"

CHILD_KEY = "DC-1"
PARENT_KEY = "DC-2"
CHILD_LOCAL = "local-child"
PARENT_LOCAL = "local-parent"


# ---------------------------------------------------------------------------
# Doubles — ONLY the binding store. Both differs and both mappers are REAL.
# ---------------------------------------------------------------------------


class _FakeBindingStore:
    """Expose bindings and the parent last observed on Jira; absent peer evidence must not
    authorize a clear."""

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

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._by_local

    def is_pending(self, local_id: str) -> bool:
        return False

    def get_baseline(self, local_id: str) -> dict[str, Any] | None:
        # No arbitration ancestor: local-wins, which is the state the live pass was in.
        return None


# ---------------------------------------------------------------------------
# The scenario — built once, shared by both cells
# ---------------------------------------------------------------------------


def _bindings() -> _FakeBindingStore:
    return _FakeBindingStore(
        {CHILD_KEY: CHILD_LOCAL, PARENT_KEY: PARENT_LOCAL},
        peer_parents={CHILD_LOCAL: PARENT_KEY},
    )


def _local_tickets() -> list[dict[str, Any]]:
    """Use a managed child and epic parent so outbound parent resolution is active."""
    child = {
        "ticket_id": CHILD_LOCAL,
        "title": "child",
        "description": "d",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
        "parent_id": PARENT_LOCAL,
        "managed_refs": [["parent", PARENT_LOCAL]],
    }
    parent = {
        "ticket_id": PARENT_LOCAL,
        "title": "parent",
        "description": "d",
        "status": "open",
        "priority": 2,
        "ticket_type": "epic",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
        "parent_id": "",
        "managed_refs": [],
    }
    return [child, parent]


def _remote_fields(summary: str, issuetype: str) -> dict[str, Any]:
    """Keep scalar fields converged so only the parent affects the diff."""
    return {
        "summary": summary,
        "description": "d",
        "issuetype": {"name": issuetype},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": None,
        "labels": [],
        "issuelinks": [],
    }


def _snapshot() -> dict[str, dict[str, Any]]:
    """Use the production parent-map merge to represent an observed Jira parent clear."""
    fetcher = importlib.import_module("rebar.reconciler.fetcher")
    base = {
        CHILD_KEY: _remote_fields("child", "Task"),
        PARENT_KEY: _remote_fields("parent", "Epic"),
    }
    return fetcher.merge_parent_map(base, {CHILD_KEY: None, PARENT_KEY: None})


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


@pytest.fixture(scope="module")
def backend() -> Any:
    from rebar.reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


def _run_outbound(
    outbound_differ: ModuleType, backend: Any, bindings: _FakeBindingStore | None = None
) -> list[Any]:
    """Run the real outbound differ; varying peer-parent evidence separates removal from local
    re-parenting."""
    mutations, _absent_alive = outbound_differ.compute_outbound_mutations(
        _local_tickets(),
        _snapshot(),
        bindings if bindings is not None else _bindings(),
        outbound_mapper=backend.outbound,
        inbound_mapper=backend.inbound,
        links=backend,
    )
    return list(mutations)


def _outbound_fields_for(mutations: list[Any], jira_key: str) -> dict[str, Any]:
    for m in mutations:
        if getattr(m, "jira_key", None) == jira_key:
            return dict(getattr(m, "fields", None) or {})
    return {}


# ---------------------------------------------------------------------------
# Diagnostic cell — link 1 in isolation
# ---------------------------------------------------------------------------


def test_outbound_does_not_re_push_a_parent_the_peer_removed(
    outbound_differ: ModuleType, backend: Any
) -> None:
    """An observed Jira parent removal must not trigger an outbound re-parent."""
    fields = _outbound_fields_for(_run_outbound(outbound_differ, backend), CHILD_KEY)
    assert "parent" not in fields, (
        f"outbound re-pushed parent={fields.get('parent')!r} for a child whose remote parent was "
        f"cleared while the observed peer parent still matches the local one. That write and the "
        f"inbound clear share an identical precondition, so inbound discards its own clear as a "
        f"same-pass contradiction and the local parent survives forever. Emitted: {fields!r}"
    )


def test_outbound_STILL_pushes_a_parent_the_local_side_re_set(
    outbound_differ: ModuleType, backend: Any
) -> None:
    """A local re-parent after the last peer observation must still be sent outbound."""
    re_set = _FakeBindingStore(
        {CHILD_KEY: CHILD_LOCAL, PARENT_KEY: PARENT_LOCAL},
        # We last observed the child under a DIFFERENT parent, so the local value is a change
        # rebar made, not a removal the peer made.
        peer_parents={CHILD_LOCAL: "DC-99"},
    )

    fields = _outbound_fields_for(_run_outbound(outbound_differ, backend, re_set), CHILD_KEY)

    assert fields.get("parent") == PARENT_KEY, (
        f"outbound did NOT push a parent the local side genuinely re-set (expected "
        f"{PARENT_KEY!r}, got {fields.get('parent')!r}). The suppression is too broad: it is "
        f"firing on a falsy remote parent alone rather than on evidence that the PEER removed "
        f"it, so real local re-parents are being dropped. Emitted: {fields!r}"
    )


# ---------------------------------------------------------------------------
# Primary cell — the two differs COMPOSED, wired the way a real pass wires them
# ---------------------------------------------------------------------------


def test_inbound_clear_survives_a_real_bidirectional_pass(
    outbound_differ: ModuleType, backend: Any
) -> None:
    """Feed real outbound mutations into inbound; an observed Jira removal must clear the local
    parent."""
    inbound_differ = importlib.import_module("rebar.reconciler.inbound_differ")

    outbound_raw = _run_outbound(outbound_differ, backend)
    ob_fields = _outbound_fields_for(outbound_raw, CHILD_KEY)

    inbound_mutations, suppressed = inbound_differ.compute_inbound_mutations(
        _snapshot(),
        _bindings(),
        {t["ticket_id"]: t for t in _local_tickets()},
        outbound_mutations=outbound_raw,
        inbound_mapper=backend.inbound,
    )
    fields_by_key = {m.jira_key: dict(m.fields or {}) for m in inbound_mutations}
    child_fields = fields_by_key.get(CHILD_KEY, {})

    assert "parent_id" in child_fields, (
        "THE INBOUND PARENT CLEAR DID NOT SURVIVE THE PASS. Jira reports no parent for "
        f"{CHILD_KEY} and every 88d9 guard is satisfied, but the bidirectional pass emitted no "
        f"parent_id clear.\n"
        f"  outbound fields for {CHILD_KEY}: {ob_fields!r}\n"
        f"  inbound fields for {CHILD_KEY}:  {child_fields!r}\n"
        f"  inbound items suppressed by outbound context: {suppressed}\n"
        "If outbound emitted a 'parent' write above, the inbound clear was computed and then "
        "dropped by the same-pass contradiction filter (_OUTBOUND_TO_INBOUND_FIELD maps "
        "'parent' -> 'parent_id'), making 88d9's clear unreachable in any bidirectional pass."
    )
    assert not child_fields["parent_id"], (
        f"the clear must remove the parent; got parent_id={child_fields['parent_id']!r}"
    )
