"""A parent REMOVED in Jira must clear the local ``parent_id`` (ticket 88d9).

TWO independent layers each dropped the signal, so the fix is two-layered and so is this
module:

  1. THE SNAPSHOT NEVER CARRIED "no parent". ``fetcher``'s parent enrichment merged
     ``client.get_parent_map()`` and, when the mapped parent was None, deliberately left the
     field ABSENT ("consistent with Jira REST shape"). A de-parented issue then looked
     identical to one that never had a parent — and identical to one whose read failed.
  2. THE DIFFER REFUSED TO EMIT A CLEAR: "We do NOT emit parent_id=None to avoid accidentally
     clearing a locally-set parent when we just can't resolve it yet". That comment names a
     REAL hazard; it was wrong only in treating ONE unresolvable case as if it covered all of
     them.

WHY THIS MODULE IS SHAPED THE WAY IT IS
---------------------------------------
An inbound CLEAR is a WRITE THAT DESTROYS LOCAL DATA, so the interesting assertions here are
the ones proving a clear is NOT emitted. ``get_parent_map`` returns ``{}`` on ANY REST failure
(``jira_datacenter/transport.py``: "a failure logs a WARNING and returns {}"), so a broken read
must never look like "no parent". The happy-path oracle alone is satisfied by an UNCONDITIONAL
clear, which is a data-loss bug — the guard oracles are the ones that protect the product.

Every snapshot in this file is built by the PRODUCTION merge, ``fetcher.merge_parent_map``, not
by hand-poking a ``parent`` key onto a finished entry. That is the whole point of layer 1's
extraction: the unobserved case is produced the way production produces it (by omitting the
issue from the map), so the guards are pinned against the real shape rather than a copy of it
that can drift.

The oracles drive PRODUCTION ENTRY POINTS — ``compute_inbound_mutations`` and
``_apply_inbound_update`` — never a private helper by name, so the pre-fix run reported "no
parent clear was emitted" rather than an AttributeError against a symbol that did not exist.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeBindingStore:
    """The lookups the inbound differ + its bidir suppression use, plus the peer-parent evidence.

    ``peer_parents`` was added after this module's original cells were written against a clear
    gated on ``managed_refs`` ALONE. That gate shipped and orphaned 63 tickets in production,
    because ``add_managed_ref`` fires on the LOCAL parent-set event and so never proved the peer
    ever had the parent (see ``test_parent_clear_requires_peer_evidence.py``). The cells that
    assert a clear DOES fire keep their intent unchanged — an observed-parentless peer must clear
    the local parent — but they now have to state the precondition their assertion always
    silently assumed: that rebar had previously OBSERVED that parent on the peer. A cell that
    supplies no evidence and still expects a clear is asserting the defect.
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
    """An ``InboundMapper`` that maps only what it is told, so the PARENT diff stands alone.

    ``mapped`` lets a cell hand back values that AGREE with the local ticket, so the scalar
    field loop emits nothing and the only field left in ``changed`` is the parent — which
    matters for the end-to-end cell, where a spurious ``title`` mutation would also be applied.
    """

    def __init__(self, mapped: dict[str, Any] | None = None) -> None:
        self._mapped = dict(mapped or {})

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        return dict(self._mapped)


class _FakeOutbound:
    """The attributes ``_build_outbound_context`` reads off an outbound mutation."""

    def __init__(self, jira_key: str, fields: dict[str, Any]) -> None:
        self.jira_key = jira_key
        self.fields = fields
        self.labels: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []


def _local(ticket_id: str, parent_id: str | None, managed: list[tuple[str, str]]) -> dict:
    """A reduced local ticket carrying ``parent_id`` and the ``managed_refs`` projection."""
    return {
        "ticket_id": ticket_id,
        "parent_id": parent_id or "",
        "deps": [],
        "managed_refs": [[kind, target] for kind, target in managed],
    }


def _snapshot(keys: list[str], parent_map: dict[str, str | None]) -> dict[str, dict[str, Any]]:
    """Build a snapshot the way ``fetcher.fetch_snapshot`` does — via the real merge.

    ``keys`` are the issues the base search returned; ``parent_map`` is what
    ``client.get_parent_map()`` answered. The CONDITIONAL inside the production merge is the
    thing under test: an issue the map does not mention never gets a ``parent`` key at all
    (unobserved — truncation, a cross-project issue, or the whole-map ``{}`` degradation),
    while an issue the map maps to None gets ``"parent": None`` (queried, authoritatively
    parentless) and may therefore be cleared.
    """
    fetcher = importlib.import_module("rebar_reconciler.fetcher")
    return fetcher.merge_parent_map({k: {} for k in keys}, parent_map)


def _inbound_fields(
    snapshot: dict[str, dict[str, Any]],
    bindings: _FakeBindingStore,
    locals_by_id: dict[str, dict[str, Any]],
    outbound: list[Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run the production inbound differ; return ``{jira_key: mutation.fields}``."""
    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")
    muts, _suppressed = inbound_differ.compute_inbound_mutations(
        snapshot,
        bindings,
        locals_by_id,
        outbound,
        inbound_mapper=_StubMapper(),
    )
    return {m.jira_key: dict(m.fields or {}) for m in muts}


def _one_managed_parent() -> tuple[_FakeBindingStore, dict[str, dict[str, Any]]]:
    """``DC-1``/``local-child`` has a MANAGED parent ``local-parent``, both bound.

    Peer evidence is part of this fixture because it is part of the STEADY STATE it models: a
    parent rebar has actually synced was OBSERVED on the peer at some point, so
    ``get_peer_parent`` returns it. The cells that expect NO clear are unaffected — each fails on
    its own independent guard (unobserved read, degraded map, unresolvable key, unmanaged ref,
    same-pass outbound write), which is exactly the property that makes them worth having.
    """
    bindings = _FakeBindingStore(
        {"DC-1": "local-child", "DC-2": "local-parent"}, peer_parents={"local-child": "DC-2"}
    )
    child = _local("local-child", "local-parent", [("parent", "local-parent")])
    parent = _local("local-parent", None, [])
    return bindings, {"local-child": child, "local-parent": parent}


# ---------------------------------------------------------------------------
# Layer 1 — the snapshot must record "queried, and there is no parent"
# ---------------------------------------------------------------------------


def test_queried_parentless_issue_gets_an_explicit_none() -> None:
    """THE LAYER-1 DEFECT. A key the map answers None for must be PRESENT with a falsy value.

    Pre-fix the field was omitted, so "Jira says no parent" was indistinguishable from "we
    never asked" — and no downstream layer could ever tell a de-parenting from a failed read.
    """
    snap = _snapshot(["DC-1"], {"DC-1": None})
    assert "parent" in snap["DC-1"], (
        "the parent key is ABSENT for an issue the parent map explicitly answered None for, so "
        f"'queried, no parent' is indistinguishable from 'never queried': {snap['DC-1']!r}"
    )
    assert not snap["DC-1"]["parent"], (
        f"expected a falsy parent for an authoritatively parentless issue; got "
        f"{snap['DC-1']['parent']!r}"
    )


def test_unmentioned_issue_gets_no_parent_key_at_all() -> None:
    """The UNOBSERVED case: an issue the map never mentions must not gain a parent key.

    This is the real fail-open shape — a truncated page walk, a cross-project issue, or a
    client with no ``get_parent_map``. If layer 1 wrote ``None`` for these too, every
    unobserved issue would authorise a clear.
    """
    snap = _snapshot(["DC-1"], {"DC-9": None})
    assert "parent" not in snap["DC-1"], (
        "an issue the parent map never mentioned gained a parent key, so a TRUNCATED read now "
        f"looks like an authoritative 'no parent': {snap['DC-1']!r}"
    )


def test_degraded_empty_parent_map_writes_nothing() -> None:
    """``get_parent_map`` returns ``{}`` on ANY REST failure; that must touch no entry."""
    snap = _snapshot(["DC-1", "DC-2"], {})
    assert all("parent" not in e for e in snap.values()), (
        f"a degraded (empty) parent map wrote parent data anyway: {snap!r}"
    )


def test_truthy_parent_keeps_the_jira_rest_shape() -> None:
    """Regression: a real parent still arrives as ``{"key": ...}`` for every consumer."""
    snap = _snapshot(["DC-1"], {"DC-1": "DC-2"})
    assert snap["DC-1"]["parent"] == {"key": "DC-2"}, (
        f"the Jira REST parent shape changed; consumers read ``parent['key']``: {snap!r}"
    )


# ---------------------------------------------------------------------------
# Layer 2, the bug: an observed-parentless issue must emit the clear
# ---------------------------------------------------------------------------


def test_observed_parentless_emits_the_clear() -> None:
    """THE DEFECT. ``DC-1`` is observed with NO parent; the managed local parent must go.

    Pre-fix nothing at all is emitted — the differ skips the None case outright — so the RED
    message reads "no parent clear was emitted", naming the missing clear rather than an
    absent symbol.
    """
    bindings, locals_by_id = _one_managed_parent()
    snap = _snapshot(["DC-1"], {"DC-1": None})

    fields = _inbound_fields(snap, bindings, locals_by_id)
    assert "parent_id" in fields.get("DC-1", {}), (
        "no parent clear was emitted. The parent map answered None for DC-1 (so Jira was "
        "asked and has NO parent) and local-child still carries the managed parent "
        f"local-parent, but the differ emitted {fields!r}"
    )
    assert not fields["DC-1"]["parent_id"], (
        f"the clear must be a falsy parent_id; got {fields['DC-1']['parent_id']!r}"
    )


# ---------------------------------------------------------------------------
# The guards — each one, independently, must decline to clear
# ---------------------------------------------------------------------------


def test_unobserved_parent_emits_no_clear() -> None:
    """GUARD 1: the snapshot has NO parent key (truncated / cross-project / unqueried).

    This is the guard that separates "Jira has no parent" from "we did not look", and it is
    the one an unconditional clear violates while still passing the happy path.
    """
    bindings, locals_by_id = _one_managed_parent()
    snap = _snapshot(["DC-1"], {"DC-9": None})  # the map never mentions DC-1

    fields = _inbound_fields(snap, bindings, locals_by_id)
    assert "parent_id" not in fields.get("DC-1", {}), (
        "a parent clear was emitted for an issue the parent map NEVER MENTIONED — a truncated "
        f"or cross-project read would silently delete the local parent: {fields!r}"
    )


def test_degraded_parent_map_emits_no_clear() -> None:
    """GUARD 1, the shape that actually happens: ``get_parent_map`` degraded to ``{}``.

    Its contract is "a failure logs a WARNING and returns {}" — so a total read failure must
    clear nothing anywhere, not merely be "unlikely".
    """
    bindings, locals_by_id = _one_managed_parent()
    snap = _snapshot(["DC-1"], {})

    fields = _inbound_fields(snap, bindings, locals_by_id)
    assert "parent_id" not in fields.get("DC-1", {}), (
        "a FAILED parent-map read (the {} degradation contract) cleared the local parent — the "
        f"exact silent data loss this ticket exists to prevent: {fields!r}"
    )


def test_unresolvable_parent_key_emits_nothing() -> None:
    """GUARD 2: the original guard's intent, preserved and independently tested.

    Jira HAS a parent but its key is not bound yet, so it resolves to no local id. Emitting a
    clear here would be exactly the "accidentally clearing a locally-set parent when we just
    can't resolve it yet" the old inline comment warned about. Nothing may be emitted; the
    next pass retries.
    """
    bindings = _FakeBindingStore({"DC-1": "local-child"})  # DC-77 is NOT bound
    child = _local("local-child", "local-parent", [("parent", "local-parent")])
    locals_by_id = {"local-child": child}
    snap = _snapshot(["DC-1"], {"DC-1": "DC-77"})

    fields = _inbound_fields(snap, bindings, locals_by_id)
    assert "parent_id" not in fields.get("DC-1", {}), (
        "an UNRESOLVABLE Jira parent key emitted a parent_id mutation. Jira has a parent — it "
        f"is merely unbound this pass — so neither a clear nor a set is correct: {fields!r}"
    )


def test_unmanaged_parent_is_adopted_not_clobbered() -> None:
    """GUARD 3 (MANAGED): a parent rebar never managed must be ADOPTED, not cleared.

    ``should_propagate_removal`` degrades to additive-only when ``managed_refs`` lacks the
    ref, and the outbound mirror's docstring states the symmetric intent outright: "a parent a
    human set directly in Jira (one local never had) would be clobbered instead of ADOPTED
    inbound".
    """
    bindings = _FakeBindingStore({"DC-1": "local-child", "DC-2": "local-parent"})
    # parent_id set locally, but NOT in managed_refs.
    locals_by_id = {
        "local-child": _local("local-child", "local-parent", []),
        "local-parent": _local("local-parent", None, []),
    }
    snap = _snapshot(["DC-1"], {"DC-1": None})

    fields = _inbound_fields(snap, bindings, locals_by_id)
    assert "parent_id" not in fields.get("DC-1", {}), (
        "an UNMANAGED parent was cleared. rebar never managed this ref, so its local presence "
        f"is not evidence we ever pushed it and the clear clobbers local state: {fields!r}"
    )


def test_same_pass_outbound_parent_write_suppresses_the_clear() -> None:
    """GUARD 4: the same-pass suppression, which covers GUARD 3's blind spot.

    ``add_managed_ref`` is folded by the parent-set EVENT, so a ref is "managed" the instant
    it is set LOCALLY — MANAGED alone does NOT prove Jira ever had the parent. In a healthy
    pass, outbound emits the parent push for exactly that ticket, and
    ``inbound_differ``'s scalar filter drops any inbound field the same pass's outbound is
    writing. So a brand-new local parent being pushed is never cleared inbound.
    """
    bindings, locals_by_id = _one_managed_parent()
    snap = _snapshot(["DC-1"], {"DC-1": None})
    # Outbound is PUSHING the parent this pass (the outbound field name is "parent").
    outbound = [_FakeOutbound("DC-1", {"parent": "DC-2"})]

    fields = _inbound_fields(snap, bindings, locals_by_id, outbound)
    assert "parent_id" not in fields.get("DC-1", {}), (
        "the inbound clear was NOT suppressed while outbound was pushing the parent in the "
        "SAME pass. The local parent is fresher than the differ snapshot, so clearing it here "
        f"destroys a parent that is mid-flight to Jira: {fields!r}"
    )


def test_outbound_parent_translates_to_the_inbound_parent_id_name() -> None:
    """GUARD 4's load-bearing detail, pinned: the outbound→inbound NAME translation.

    The suppression above compares inbound field names against the outbound mutation's field
    names, and the two directions name this field DIFFERENTLY — outbound "parent", inbound
    "parent_id". The protection therefore rests entirely on
    ``_OUTBOUND_TO_INBOUND_FIELD``. Without this cell, deleting that entry would silently
    remove the guard while every other test still passed.
    """
    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")
    assert inbound_differ._OUTBOUND_TO_INBOUND_FIELD.get("parent") == "parent_id", (
        "the outbound->inbound field translation no longer maps 'parent' to 'parent_id', so "
        "the same-pass suppression silently stops firing for the parent field and an inbound "
        "clear can destroy a parent outbound is pushing in the same pass. Map is "
        f"{inbound_differ._OUTBOUND_TO_INBOUND_FIELD!r}"
    )
    ctx = inbound_differ._build_outbound_context([_FakeOutbound("DC-1", {"parent": "DC-2"})])
    assert "parent_id" in ctx["DC-1"]["fields"], (
        "the outbound context did not record the parent write under the INBOUND field name, "
        f"so the scalar filter cannot match it: {ctx!r}"
    )


# ---------------------------------------------------------------------------
# Steady state — no churn, and every existing consumer unaffected
# ---------------------------------------------------------------------------


def test_never_parented_issue_never_emits_a_parent_mutation() -> None:
    """An issue with no parent on EITHER side emits nothing, on repeated passes.

    This is also the consumer-safety oracle for layer 1's explicit None: the field is now
    always present for a queried issue, and a ticket that never had a parent must still
    produce no parent mutation rather than a per-pass ``parent_id=""`` churn.
    """
    bindings = _FakeBindingStore({"DC-1": "local-child"})
    locals_by_id = {"local-child": _local("local-child", None, [])}
    snap = _snapshot(["DC-1"], {"DC-1": None})

    for pass_no in (1, 2, 3):
        fields = _inbound_fields(snap, bindings, locals_by_id)
        assert "parent_id" not in fields.get("DC-1", {}), (
            f"pass {pass_no} emitted a parent mutation for a ticket that never had a parent — "
            f"this would churn every pass. fields={json.dumps(fields, default=str)}"
        )


def test_unchanged_parent_does_not_churn() -> None:
    """A parent that agrees on both sides emits nothing, on repeated passes."""
    bindings, locals_by_id = _one_managed_parent()
    snap = _snapshot(["DC-1"], {"DC-1": "DC-2"})

    for pass_no in (1, 2, 3):
        fields = _inbound_fields(snap, bindings, locals_by_id)
        assert "parent_id" not in fields.get("DC-1", {}), (
            f"pass {pass_no} emitted a parent mutation for an UNCHANGED parent. "
            f"fields={json.dumps(fields, default=str)}"
        )


def test_reparent_still_emits_the_new_parent() -> None:
    """Regression: the SET path (a Jira re-parent) is untouched by the three-state read."""
    bindings = _FakeBindingStore({"DC-1": "local-child", "DC-2": "local-a", "DC-3": "local-b"})
    locals_by_id = {
        "local-child": _local("local-child", "local-a", [("parent", "local-a")]),
        "local-a": _local("local-a", None, []),
        "local-b": _local("local-b", None, []),
    }
    snap = _snapshot(["DC-1"], {"DC-1": "DC-3"})

    fields = _inbound_fields(snap, bindings, locals_by_id)
    assert fields.get("DC-1", {}).get("parent_id") == "local-b", (
        f"a Jira re-parent no longer reaches the local ticket: {fields!r}"
    )


def test_explicit_none_leaves_the_canonical_remote_parent_unchanged() -> None:
    """Consumer check: ``inbound_fields`` is the ONE consumer that keys on ``"parent" in``.

    ``_map_jira_to_local_fields`` gates ``remote_parent_id`` on key PRESENCE, so layer 1's
    explicit None does change its output shape (the key appears, valued None) — but its only
    consumer, the outbound field diff, reads it with ``.get("remote_parent_id")``, for which
    absent and None are the same value. Pinned here so that equivalence is not merely argued.
    """
    inbound_fields = importlib.import_module("rebar_reconciler.inbound_fields")
    explicit = inbound_fields._map_jira_to_local_fields({"parent": None})
    absent = inbound_fields._map_jira_to_local_fields({})
    assert explicit.get("remote_parent_id") == absent.get("remote_parent_id") is None, (
        "an explicitly-None snapshot parent no longer reads as 'no remote parent' through the "
        f"canonical mapper: explicit={explicit!r} absent={absent!r}"
    )


# ---------------------------------------------------------------------------
# End to end — the clear must survive to the REDUCED ticket
# ---------------------------------------------------------------------------


@pytest.fixture
def parented_tickets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """A real initialised store where ``child`` has ``parent`` as its parent."""
    import rebar

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.com"),
        ("git", "config", "user.name", "d"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    parent = str(rebar.create_ticket("task", "88d9 parent", repo_root=repo))
    child = str(rebar.create_ticket("task", "88d9 child", parent=parent, repo_root=repo))
    return repo, parent, child


def test_end_to_end_a_removed_jira_parent_leaves_parent_id_empty(
    parented_tickets: tuple[Path, str, str],
) -> None:
    """THE ACCEPTANCE ORACLE: differ emit -> payload -> apply -> ``parent_id`` POSITIVELY EMPTY.

    A differ-boundary oracle is NOT sufficient, and that is the whole reason this cell exists:
    the emitted value is ``None``, which is exactly what an intermediate ``if v`` or a
    falsy-filtering dict comprehension drops in silence. The payload here is built the way
    ``run_differs`` builds it (``"fields": im.fields``) and handed to the production
    ``_apply_inbound_update``, so every layer between the differ and the store is traversed.

    "The apply call did not raise" is deliberately NOT the oracle. The only thing read is
    whether the reduced ticket's ``parent_id`` is empty.
    """
    import rebar

    repo, parent, child = parented_tickets

    # SETUP, asserted: the parent and its managed ref must really exist, or absence is vacuous.
    child_ticket = rebar.show_ticket(child, repo_root=repo)
    assert child_ticket.get("parent_id") == parent, (
        f"SETUP FAILED: {child} does not carry parent_id={parent!r} "
        f"(got {child_ticket.get('parent_id')!r})"
    )
    managed = [list(m) for m in (child_ticket.get("managed_refs") or [])]
    assert ["parent", parent] in managed, (
        f"SETUP FAILED: ('parent', {parent}) is not in managed_refs={managed!r}; the removal "
        f"gate would decline and this cell would pass for the wrong reason"
    )

    # Peer evidence: rebar had OBSERVED this child carrying DC-2 as its parent, so the peer's
    # present parentlessness is a real de-parenting rather than a parent we never pushed.
    bindings = _FakeBindingStore({"DC-1": child, "DC-2": parent}, peer_parents={child: "DC-2"})
    locals_by_id = {child: child_ticket, parent: rebar.show_ticket(parent, repo_root=repo)}
    # The issue was OBSERVED and Jira has no parent for it.
    snap = _snapshot(["DC-1"], {"DC-1": None})

    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")
    muts, _ = inbound_differ.compute_inbound_mutations(
        snap,
        bindings,
        locals_by_id,
        None,
        # Agree with local on every scalar so ``parent_id`` is the ONLY applied field.
        inbound_mapper=_StubMapper({"title": child_ticket.get("title")}),
    )
    assert muts and "parent_id" in muts[0].fields, (
        f"the differ emitted no parent clear, so the apply layer cannot be exercised: "
        f"{[m.fields for m in muts]!r}"
    )
    assert muts[0].fields["parent_id"] is None, (
        f"expected the clear to travel as None; got {muts[0].fields['parent_id']!r}"
    )

    mut_mod = importlib.import_module("rebar_reconciler.mutation")
    apply_inbound = importlib.import_module("rebar_reconciler.apply_inbound")
    typed = mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target=muts[0].jira_key,
        # The payload shape run_differs builds: the differ's fields dict, VERBATIM.
        payload={"local_id": muts[0].local_id, "fields": muts[0].fields},
        provenance={"source": "inbound_differ"},
    )
    result = apply_inbound._apply_inbound_update(typed, client=None, repo_root=repo)

    after = rebar.show_ticket(child, repo_root=repo).get("parent_id")
    assert not after, (
        f"THE PARENT IS STILL THERE. Jira reported no parent and the differ emitted the clear, "
        f"but {child} still carries parent_id={after!r}. The None was dropped somewhere between "
        f"the differ and the store; apply result={result.payload if result else result!r}"
    )
