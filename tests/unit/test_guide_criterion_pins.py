"""Gate parity between the packaged prose guides and the criteria registry (ticket 2f3c).

Bug 828a: `writing-a-passing-plan.md` told authors to name a rejected alternative while the
authoritative G6 criterion explicitly says NOT to require one. The drift shipped for a month.
`validate_criteria_guide()` covers only the GENERATED `docs/plan-review-criteria-guide.md`;
the packaged prose guides cite criterion ids inline as if authoritative with nothing coupling
them to the registry. The 828a fix added hand-written G6-specific assertions — good for that
one claim, but 14 other cited ids in the plan guide (and every citation added tomorrow) still
have no coupling.

A gate cannot judge whether prose faithfully paraphrases a rubric. What it CAN do is refuse to
let a cited criterion change SILENTLY: pin a digest of every cited criterion's registry text,
and fail when the criterion moves out from under the prose. These tests pin that contract —
the pure `compute_pins`/`diff_pins` core (so a criterion mutation needs no monkeypatching) and
the packaged gate wired into `validate-routing`.
"""

from __future__ import annotations

import json

import pytest

from rebar.llm.plan_review import guide_parity
from rebar.llm.plan_review import registry as plan_registry

_VOCAB = ("G6", "F1", "E2", "T1", "T10", "T5a", "COH")

_GUIDE = "writing-a-passing-plan.md"
_OTHER_GUIDE = "passing-code-review.md"


def _criterion(cid: str, scenario: str = "Original scenario text.") -> dict:
    return {
        "id": cid,
        "name": f"Criterion {cid}",
        "exec": "1-TURN",
        "facet": "structure",
        "default_posture": "advisory",
        "scenario": scenario,
        "checklist": [],
    }


def _criteria(*ids: str) -> dict[str, dict]:
    return {cid: _criterion(cid) for cid in ids}


# ── citation extraction: the closed-vocabulary, structure-free interface ─────────────
def test_extracts_bare_and_backticked_citations() -> None:
    text = "how you'll solve it (G6), and be executable (`E2`)."
    assert guide_parity.cited_criteria(text, _VOCAB) == ("E2", "G6")


def test_extraction_is_word_bounded_so_prefixes_do_not_collide() -> None:
    """`T1` must not be matched inside `T10`, and vice versa — the ids share a prefix."""
    assert guide_parity.cited_criteria("see T10 for detail", _VOCAB) == ("T10",)
    assert guide_parity.cited_criteria("see T1 for detail", _VOCAB) == ("T1",)


def test_extraction_ignores_ids_outside_the_vocabulary() -> None:
    """The vocabulary is CLOSED, so prose that happens to contain an id-shaped token for
    something else does not become a phantom citation."""
    assert guide_parity.cited_criteria("ticket G9 and step Z1", _VOCAB) == ()


def test_extraction_deduplicates_and_sorts() -> None:
    assert guide_parity.cited_criteria("G6 ... G6 ... F1", _VOCAB) == ("F1", "G6")


def test_extraction_depends_on_no_guide_structure() -> None:
    """Reordering/rewrapping prose must not change the extracted set — the gate keys on the
    id vocabulary, never on headings, fences, or layout."""
    a = "## Heading\n\n- **Approach (G6).** Text.\n\n```md\nfenced `F1`\n```\n"
    b = "Totally different prose mentioning F1 first, then G6, with no headings at all."
    assert guide_parity.cited_criteria(a, _VOCAB) == guide_parity.cited_criteria(b, _VOCAB)


# ── the pin manifest ────────────────────────────────────────────────────────────────
def test_compute_pins_is_keyed_by_guide_then_criterion() -> None:
    pins = guide_parity.compute_pins({_GUIDE: "uses G6 and F1"}, _criteria("G6", "F1"))

    assert pins["schema_version"] == guide_parity.PINS_SCHEMA_VERSION
    assert sorted(pins["guides"][_GUIDE]) == ["F1", "G6"]
    assert all(isinstance(v, str) and v for v in pins["guides"][_GUIDE].values())


def test_pins_are_canonical_and_idempotent() -> None:
    guides = {_GUIDE: "G6 F1", _OTHER_GUIDE: "E2"}
    criteria = _criteria("G6", "F1", "E2")
    first = guide_parity.compute_pins(guides, criteria)

    assert first == guide_parity.compute_pins(guides, criteria)
    assert json.dumps(first, sort_keys=True) == json.dumps(
        guide_parity.compute_pins(guides, criteria), sort_keys=True
    )


def test_digest_changes_when_the_criterion_text_changes() -> None:
    before = guide_parity.criterion_digest(_criterion("G6", "Original scenario text."))
    after = guide_parity.criterion_digest(_criterion("G6", "Rewritten scenario text."))
    assert before != after


# ── the five problem classes ────────────────────────────────────────────────────────
def test_in_sync_pins_report_no_problems() -> None:
    guides = {_GUIDE: "G6 and F1"}
    criteria = _criteria("G6", "F1")
    pins = guide_parity.compute_pins(guides, criteria)

    assert guide_parity.diff_pins(pins, guides, criteria) == []


def test_mutating_a_cited_criterion_reports_a_stale_pin(caplog) -> None:
    """The 828a class: the criterion's text moves out from under the prose that paraphrases
    it. The gate must name BOTH the criterion and the guide that cites it."""
    guides = {_GUIDE: "how you'll solve it (G6)"}
    pins = guide_parity.compute_pins(guides, _criteria("G6"))

    mutated = {"G6": _criterion("G6", "A rewritten rule that contradicts the guide.")}
    problems = guide_parity.diff_pins(pins, guides, mutated)

    assert len(problems) == 1
    assert "G6" in problems[0]
    assert _GUIDE in problems[0]
    assert "stale" in problems[0].lower()


def test_stale_detection_covers_every_author_guide_not_just_the_plan_guide() -> None:
    """Parameterized over the real AUTHOR_GUIDES map: whichever guide carries the citation,
    a criterion change is reported against that guide."""
    for filename in plan_registry.AUTHOR_GUIDES.values():
        guides = {filename: "cites G6 here"}
        pins = guide_parity.compute_pins(guides, _criteria("G6"))
        problems = guide_parity.diff_pins(pins, guides, {"G6": _criterion("G6", "changed text")})
        assert problems and filename in problems[0], f"{filename} not covered by the gate"


def test_a_new_citation_with_no_pin_is_reported_as_unpinned() -> None:
    guides = {_GUIDE: "G6 only"}
    criteria = _criteria("G6", "F1")
    pins = guide_parity.compute_pins(guides, criteria)
    guides = {_GUIDE: "G6 and now also F1"}

    problems = guide_parity.diff_pins(pins, guides, criteria)

    assert len(problems) == 1
    assert "F1" in problems[0] and _GUIDE in problems[0]
    assert "unpinned" in problems[0].lower()


def test_a_pin_for_a_retired_criterion_is_reported() -> None:
    """A renamed/removed criterion: the guide still cites the old id, which has left the
    registry vocabulary, so the guide itself must be updated."""
    guides = {_GUIDE: "cites G6"}
    pins = guide_parity.compute_pins(guides, _criteria("G6"))

    problems = guide_parity.diff_pins(pins, guides, _criteria("F1"))

    assert len(problems) == 1
    assert "G6" in problems[0] and _GUIDE in problems[0]
    assert "retired" in problems[0].lower()


def test_a_pin_no_longer_cited_is_reported_as_orphan() -> None:
    guides = {_GUIDE: "cites G6 and F1"}
    criteria = _criteria("G6", "F1")
    pins = guide_parity.compute_pins(guides, criteria)
    guides = {_GUIDE: "now cites only G6"}

    problems = guide_parity.diff_pins(pins, guides, criteria)

    assert len(problems) == 1
    assert "F1" in problems[0] and _GUIDE in problems[0]
    assert "orphan" in problems[0].lower()


def test_prose_edit_that_keeps_the_citations_is_not_a_failure() -> None:
    """Negative control: the gate must not fire on ordinary guide rewording, or authors will
    learn to regenerate reflexively without re-verifying anything."""
    criteria = _criteria("G6", "F1")
    pins = guide_parity.compute_pins({_GUIDE: "old wording about G6 and F1"}, criteria)

    rewritten = {_GUIDE: "## New heading\n\nCompletely rewritten prose. F1 first, then G6.\n"}
    assert guide_parity.diff_pins(pins, rewritten, criteria) == []


def test_missing_manifest_is_reported_with_the_remedy() -> None:
    problems = guide_parity.diff_pins(None, {_GUIDE: "G6"}, _criteria("G6"))

    assert len(problems) == 1
    assert "regenerate" in problems[0].lower()


# ── the packaged gate, over the real guides + registry ──────────────────────────────
def test_committed_manifest_is_in_sync_with_the_registry() -> None:
    """The drift gate itself: the shipped pins must match the shipped criteria."""
    assert guide_parity.validate_guide_criterion_pins() == []


def test_real_plan_guide_cites_criteria_that_are_actually_pinned() -> None:
    """Guards against a vacuous gate: if extraction silently found nothing, every other test
    here would pass while the gate protected nothing."""
    pins = guide_parity.load_guide_pins()
    plan_pins = pins["guides"][plan_registry.AUTHOR_GUIDES["plan"]]

    assert "G6" in plan_pins, "the plan guide's G6 citation is not pinned"
    assert len(plan_pins) >= 10, f"suspiciously few pinned citations: {sorted(plan_pins)}"


def test_regeneration_is_idempotent() -> None:
    first = guide_parity.build_guide_pins()
    assert first == guide_parity.build_guide_pins()


# ── CI wiring: composed into validate-routing, no new workflow step ─────────────────
def test_validate_routing_surfaces_a_pin_problem(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "rebar.llm.plan_review.guide_parity.validate_guide_criterion_pins",
        lambda *a, **k: ["pin gate: G6 is stale in writing-a-passing-plan.md"],
    )

    rc = plan_registry._main(["validate-routing"])

    assert rc == 1
    assert "G6 is stale" in capsys.readouterr().err


def test_validate_routing_passes_when_the_pin_gate_is_clean(monkeypatch) -> None:
    """Composition, isolated from its siblings. A bare ``_main(["validate-routing"])`` cannot
    be asserted green here: ``validate_criteria_guide`` resolves ``docs/`` against the AMBIENT
    ``config.repo_root()``, which the unit-test sandbox redirects to a tmp dir, so it reports
    a missing generated guide on a PRISTINE tree too (verified with this module absent). That
    is pre-existing and unrelated to the pin gate — so stub the siblings and assert that a
    clean pin gate leaves the composition green."""
    monkeypatch.setattr(plan_registry, "validate_packaged_routing", lambda *a, **k: [])
    monkeypatch.setattr(plan_registry, "validate_criteria_guide", lambda *a, **k: [])

    assert plan_registry._main(["validate-routing"]) == 0
    assert guide_parity.validate_guide_criterion_pins() == []


@pytest.mark.parametrize("cmd", ["regenerate-guide-pins", "validate-guide-pins"])
def test_guide_parity_main_supports_its_commands(cmd, monkeypatch, tmp_path) -> None:
    if cmd == "regenerate-guide-pins":
        monkeypatch.setattr(
            guide_parity, "regenerate_guide_criterion_pins", lambda: str(tmp_path / "p.json")
        )
    assert guide_parity._main([cmd.replace("-guide-pins", "")]) == 0
