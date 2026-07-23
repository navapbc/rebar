"""Signing must survive UNRELATED concurrent tracker writes.

Bug (client report §2): PlanReviewGeneration equality compared the whole store —
ticket_store_revision (the store-wide tracker HEAD) and relation_snapshot.ticket_states_by_id
(every ticket's state). initial_generation is captured at review START; the sign-time `fresh`
is collected minutes later. Any commit to ANY ticket in that window (an unrelated agent's
comment/claim/transition) made fresh != initial_generation, raising PlanReviewGenerationChanged
and discarding the completed (billed) review — even with before == after within the attempt.

Generation identity must be scoped to what the manifest actually binds: the subject's own
material + its DIRECT related material (child/prerequisite pins) + phase/floor.

Generations are built via ``from_snapshot`` so the tests are agnostic to the dataclass's exact
field set (they exercise the equality contract, not its representation).
"""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import generation
from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin, PlanRelationSnapshot

TICKET = "1111-2222-3333-4444"


def _gen(*, store, revision, pins=(), desc="plan"):
    snapshot = PlanRelationSnapshot(
        subject_state={"ticket_id": TICKET, "status": "in_progress", "description": desc},
        ticket_states_by_id=store,
        child_ids=(),
        prerequisite_ids=(),
        related_material=tuple(pins),
        ticket_store_revision=revision,
    )
    return generation.from_snapshot(snapshot)


def _wire(monkeypatch, fresh):
    monkeypatch.setattr(generation.config, "tracker_dir", lambda root=None: "/tracker")
    # Stable head within the attempt: before == after (the reported failure signature).
    monkeypatch.setattr(generation, "tracker_head_sha", lambda *a, **k: "b" * 40)
    monkeypatch.setattr(generation, "collect", lambda *a, **k: fresh)

    def signer(ticket_id, manifest, **kwargs):
        kwargs["under_lock_check"]()
        return {"signed": True}

    monkeypatch.setattr("rebar.signing._sign_manifest_under_lock", signer)


def test_unrelated_store_churn_does_not_abort_signing(monkeypatch) -> None:
    # Same subject material, same (empty) related material — only UNRELATED store state moved.
    initial = _gen(store={"9999-9999-9999-9999": {"status": "open"}}, revision="a" * 40)
    fresh = _gen(store={"9999-9999-9999-9999": {"status": "closed"}}, revision="z" * 40)
    _wire(monkeypatch, fresh)
    # Pre-fix: fresh != initial (store-wide compare) -> PlanReviewGenerationChanged. Post-fix signs.
    assert generation.sign_manifest(TICKET, ["plan-review: PASS"], initial) == {"signed": True}


def test_subject_material_change_still_aborts(monkeypatch) -> None:
    initial = _gen(store={}, revision="a" * 40, desc="plan")
    fresh = _gen(store={}, revision="a" * 40, desc="materially different plan")
    _wire(monkeypatch, fresh)
    with pytest.raises(generation.PlanReviewGenerationChanged):
        generation.sign_manifest(TICKET, ["plan-review: PASS"], initial)


def test_related_material_change_still_aborts(monkeypatch) -> None:
    pin_old = PlanMaterialPin("prerequisite", "cccc-cccc-cccc-cccc", "fp-old")
    pin_new = PlanMaterialPin("prerequisite", "cccc-cccc-cccc-cccc", "fp-new")
    initial = _gen(store={}, revision="a" * 40, pins=(pin_old,))
    # A DIRECT prerequisite's material fingerprint changed during the window.
    fresh = _gen(store={}, revision="a" * 40, pins=(pin_new,))
    _wire(monkeypatch, fresh)
    with pytest.raises(generation.PlanReviewGenerationChanged):
        generation.sign_manifest(TICKET, ["plan-review: PASS"], initial)
