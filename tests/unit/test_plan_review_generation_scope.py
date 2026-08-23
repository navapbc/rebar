"""Signing must survive UNRELATED concurrent tracker writes.

Bug (client report §2): PlanReviewGeneration equality compared the whole store —
ticket_store_revision (the store-wide tracker HEAD) and relation_snapshot.ticket_states_by_id
(every ticket's state). initial_generation is captured at review START; the sign-time `fresh`
is collected minutes later. Any commit to ANY ticket in that window (an unrelated agent's
comment/claim/transition) made fresh != initial_generation, raising PlanReviewGenerationChanged
and discarding the completed (billed) review — even with before == after within the attempt.

Bug a83f: the d70a fix scoped generation IDENTITY to the subject's material but left
sign_manifest's within-attempt fence store-wide (`before != after` HEAD samples around
collect(), and under_lock_check's `locked_head != expected_after`), so signing still demanded
a globally quiescent tracker it never gets under normal churn — and the regression tests here
hid it by freezing ``tracker_head_sha`` to a constant. These tests therefore run against a
REAL temp git tracker and really commit unrelated events inside the window; nothing patches
``tracker_head_sha``.

Generation identity must be scoped to what the manifest actually binds: the subject's own
material + its DIRECT related material (child/prerequisite pins) + phase/floor.

Generations are built via ``from_snapshot`` so the tests are agnostic to the dataclass's exact
field set (they exercise the equality contract, not its representation).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    """A real git tracker: the store-wide HEAD moves only via REAL commits."""
    root = tmp_path / "tracker"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "seed.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)
    return root


def _commit_unrelated_event(tracker: Path, name: str) -> None:
    """Land a REAL unrelated ticket event commit in the shared tracker."""
    (tracker / f"{name}.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
    subprocess.run(["git", "commit", "-qm", f"ticket: COMMENT {name}"], cwd=tracker, check=True)


def _wire(monkeypatch, tracker: Path, collect=None, fresh=None, signer=None):
    monkeypatch.setattr(generation, "collect", collect or (lambda *a, **k: fresh))
    if signer is None:

        def signer(ticket_id, manifest, **kwargs):
            kwargs["under_lock_check"]()
            return {"signed": True}

    monkeypatch.setattr("rebar.signing._sign_manifest_under_lock", signer)


def test_unrelated_store_churn_does_not_abort_signing(monkeypatch, tracker) -> None:
    # Same subject material, same (empty) related material — only UNRELATED store state moved.
    initial = _gen(store={"9999-9999-9999-9999": {"status": "open"}}, revision="a" * 40)
    fresh = _gen(store={"9999-9999-9999-9999": {"status": "closed"}}, revision="z" * 40)
    _wire(monkeypatch, tracker, fresh=fresh)
    # Pre-fix: fresh != initial (store-wide compare) -> PlanReviewGenerationChanged. Post-fix signs.
    assert generation.sign_manifest(TICKET, ["plan-review: PASS"], initial) == {"signed": True}


def test_signs_while_unrelated_commits_land_mid_attempt(monkeypatch, tracker) -> None:
    """Bug a83f site A: an unrelated ticket event REALLY committing inside every attempt's
    collect() window must not starve signing (pre-fix: three `before != after` retries,
    then `plan review generation remained unstable after 3 attempts`)."""
    initial = _gen(store={}, revision="a" * 40)
    calls = {"n": 0}

    def churning_collect(*a, **k):
        calls["n"] += 1
        _commit_unrelated_event(tracker, f"unrelated-{calls['n']}")
        return _gen(store={}, revision="a" * 40)

    _wire(monkeypatch, tracker, collect=churning_collect)
    assert generation.sign_manifest(TICKET, ["plan-review: PASS"], initial) == {"signed": True}


def test_signs_when_unrelated_commit_lands_during_lock_acquisition(monkeypatch, tracker) -> None:
    """Bug a83f site B: an unrelated commit landing between the pre-lock sample and lock
    acquisition must not invalidate the attempt (pre-fix: `locked_head != expected_after`
    is anti-correlated with lock waits — three under-lock mismatches, then exhaustion)."""
    initial = _gen(store={}, revision="a" * 40)

    def signer(ticket_id, manifest, **kwargs):
        _commit_unrelated_event(tracker, f"during-lock-{ticket_id[:4]}")
        kwargs["under_lock_check"]()
        return {"signed": True}

    _wire(monkeypatch, tracker, fresh=_gen(store={}, revision="a" * 40), signer=signer)
    assert generation.sign_manifest(TICKET, ["plan-review: PASS"], initial) == {"signed": True}


def test_subject_material_change_still_aborts(monkeypatch, tracker) -> None:
    initial = _gen(store={}, revision="a" * 40, desc="plan")
    fresh = _gen(store={}, revision="a" * 40, desc="materially different plan")
    _wire(monkeypatch, tracker, fresh=fresh)
    with pytest.raises(generation.PlanReviewGenerationChanged):
        generation.sign_manifest(TICKET, ["plan-review: PASS"], initial)


def test_related_material_change_still_aborts(monkeypatch, tracker) -> None:
    pin_old = PlanMaterialPin("prerequisite", "cccc-cccc-cccc-cccc", "fp-old")
    pin_new = PlanMaterialPin("prerequisite", "cccc-cccc-cccc-cccc", "fp-new")
    initial = _gen(store={}, revision="a" * 40, pins=(pin_old,))
    # A DIRECT prerequisite's material fingerprint changed during the window.
    fresh = _gen(store={}, revision="a" * 40, pins=(pin_new,))
    _wire(monkeypatch, tracker, fresh=fresh)
    with pytest.raises(generation.PlanReviewGenerationChanged):
        generation.sign_manifest(TICKET, ["plan-review: PASS"], initial)
