"""Is 88d9's inbound parent CLEAR reachable in a REAL bidirectional pass? (37e7-d751-0042-4b94)

THE SUSPECTED DEFECT. A live harness cell cleared a child's Epic Link on the Jira side (two
independent reads confirmed the parent was gone) and the next inbound pass did NOT clear the
local ``parent_id``. The proposed mechanism is SAME-PASS OUTBOUND SUPPRESSION, in two links:

  1. The OUTBOUND differ still resolves the local parent (``_resolve_local_parent`` in
     ``outbound_field_diff.py`` returns the bound epic parent's remote key) and, because the
     canonicalized remote now says ``remote_parent_id is None``, the two differ — so local-wins
     emits ``changed["parent"] = <remote key>``. ``parent`` is NOT in
     ``_INBOUND_MIRRORED_FIELDS``, so no baseline arbitration can defer to inbound here.
  2. The INBOUND differ canonicalizes that outbound ``parent`` to the inbound name ``parent_id``
     (``_OUTBOUND_TO_INBOUND_FIELD``) and then DROPS every inbound field the same pass's
     outbound is writing. The clear 88d9 built is computed and immediately discarded.

If both links hold, 88d9's clear is unreachable in ANY bidirectional pass whenever rebar can
resolve the parent outbound — which is the normal case for an epic parent.

WHY THIS TIER, AND WHY THE EXISTING CELLS COULD NOT SEE IT
----------------------------------------------------------
This is a DIFFER-COMPOSITION defect: neither differ is individually wrong: outbound's local-wins
SET and inbound's contradiction filter are each the documented, intended behaviour. The bug (if
it is one) lives only in their COMPOSITION, so it is invisible to any cell that exercises one
differ alone. That is exactly what the existing 88d9 coverage does:

  * ``test_inbound_parent_clear_88d9.py`` and ``test_parent_clear_requires_peer_evidence.py``
    pass ``outbound_mutations=None`` — a pass with no outbound context at all;
  * the one cell that DOES supply outbound context,
    ``test_same_pass_outbound_parent_write_suppresses_the_clear``, HAND-BUILDS the outbound
    entry (``_FakeOutbound("DC-1", {"parent": "DC-2"})``). It asserts that a hypothetical
    outbound parent write suppresses the clear; it never asks whether the REAL outbound differ
    produces that write in the de-parented scenario. The hand-built double is precisely the
    thing under test here, so this module must not use one.

So the load-bearing property of this module is the WIRING: the real ``compute_outbound_mutations``
output is fed verbatim into ``compute_inbound_mutations`` as ``outbound_mutations``, mirroring
``run_differs._run_differs_inbound``. A unit tier is right because both differs are pure
functions of (local tickets, snapshot, binding store) — no transport, no store — so the whole
composition runs in-process with real mappers and only the binding store doubled.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"

CHILD_KEY = "DC-1"
PARENT_KEY = "DC-2"
CHILD_LOCAL = "local-child"
PARENT_LOCAL = "local-parent"


def _load(name: str, filename: str) -> ModuleType:
    """Load an engine module by path — the loader convention of this test directory."""
    spec = importlib.util.spec_from_file_location(name, ENGINE / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Doubles — ONLY the binding store. Both differs and both mappers are REAL.
# ---------------------------------------------------------------------------


class _FakeBindingStore:
    """Every binding lookup the two differs make, including 88d9's peer-parent evidence.

    ``peer_parents`` records the parent rebar last OBSERVED on the peer. It is populated here
    because the scenario is a genuine de-parenting of a parent rebar really had synced — without
    it, ``test_parent_clear_requires_peer_evidence.py``'s gate declines the clear and this module
    would go green for a reason that has nothing to do with outbound suppression.
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
    """The child (parented, managed) and its EPIC parent.

    The parent's ``ticket_type`` is ``epic`` deliberately: ``_resolve_local_parent`` omits a
    non-epic parent outright (ticket 8b25), so a task parent would make link 1 unreachable for a
    reason unrelated to the mechanism under test. Epic is also what the live harness cell used.
    """
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
    """A Jira snapshot entry that AGREES with its local ticket on every scalar.

    Agreement matters: the question is whether the PARENT field alone drives the suppression,
    so any spurious scalar divergence would put an unrelated field in the outbound mutation and
    muddy the diagnostic.
    """
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
    """The de-parented remote state, built by the PRODUCTION merge (``fetcher.merge_parent_map``).

    ``{CHILD_KEY: None}`` is the "queried, and Jira has no parent" three-state that 88d9 layer 1
    introduced — the Epic-Link-cleared state the live harness observed. Hand-poking
    ``"parent": None`` onto the entry would let the snapshot shape drift away from production.
    """
    fetcher = importlib.import_module("rebar_reconciler.fetcher")
    base = {
        CHILD_KEY: _remote_fields("child", "Task"),
        PARENT_KEY: _remote_fields("parent", "Epic"),
    }
    return fetcher.merge_parent_map(base, {CHILD_KEY: None, PARENT_KEY: None})


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load("outbound_differ_bidir_37e7", "outbound_differ.py")


@pytest.fixture(scope="module")
def backend() -> Any:
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


def _run_outbound(
    outbound_differ: ModuleType, backend: Any, bindings: _FakeBindingStore | None = None
) -> list[Any]:
    """The REAL outbound differ over the de-parented scenario; returns its raw mutations.

    No ``client`` is supplied, mirroring the unit path: every key in the scenario IS in the
    snapshot, so the bound-but-absent direct-GET branch is never reached.

    ``bindings`` is injectable so a cell can vary the OBSERVED PEER PARENT while holding the rest
    of the scenario fixed. That single value is what separates "the peer removed the parent" from
    "the local side re-set it", and the two must produce opposite outbound behaviour.
    """
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
    """LINK 1, INVERTED: outbound must stay SILENT when the peer removed the parent.

    THIS CELL WAS WRITTEN THE OTHER WAY UP, and the flip is deliberate rather than a fix to a
    broken test. While the defect existed it characterised link 1 — it asserted that outbound
    DOES emit ``parent`` here, which is what let the primary cell's red be attributed to the
    suppression rather than to one of 88d9's own guards declining. Removing the defect
    necessarily falsifies a cell that characterises the defect, so it could not survive
    unchanged: its old assertion and the fix are contradictory over the same fixture by
    construction.

    Re-aimed at the property the fix establishes, it becomes a genuine regression guard, and a
    sharper one than the primary cell. The primary asserts the OUTCOME (the local parent ends up
    cleared); this asserts the MECHANISM that produces it (outbound never emits the contradicting
    write). Restoring the re-push would redden this cell immediately, whereas the primary could
    in principle be rescued by some other change to inbound's contradiction filter.
    """
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
    """THE OPPOSITE DIRECTION, and the cell that stops the fix becoming a worse bug.

    Suppressing the outbound push whenever the remote has no parent would be trivially sufficient
    to green every other cell here — and it would silently discard real local intent, turning
    "the peer wins a removal it actually made" into "the peer always wins". That is a data-loss
    bug strictly worse than the one being fixed.

    The discriminator is the OBSERVED PEER PARENT. Here it is a DIFFERENT key from the local
    parent, which is what "the local side re-parented since we last looked" looks like on disk —
    identical to the suppressed scenario in every other respect, including the remote being
    parentless. Outbound must push.
    """
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
    """THE PROPERTY UNDER TEST: after a full bidirectional pass, the local parent is cleared.

    Jira is authoritatively parentless for this child, rebar had OBSERVED the parent on the peer,
    and the ref is managed — every one of 88d9's guards is satisfied, so the clear is the correct
    outcome. The wiring is what makes this cell different from every existing 88d9 cell: the real
    outbound differ's mutations are handed to ``compute_inbound_mutations`` as
    ``outbound_mutations``, exactly as ``run_differs._run_differs_inbound`` does. Nothing about
    the outbound entry is invented here.

    Without this cell, the whole 88d9 feature could be — and appears to have been — dead in every
    bidirectional pass while its entire unit suite stayed green, because every one of those cells
    either omits outbound context or supplies a hand-built stand-in for it.
    """
    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")

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
