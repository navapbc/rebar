"""Exercise the advisory ``ac-satisfiability`` plan-review criterion.

The criterion detects contradictions among one ticket's acceptance criteria, a scope
not covered by cross-section coherence, work mapping, or per-criterion measurability.
Tests verify generic rubric text, registry metadata, and Pass 1 execution.
"""

from __future__ import annotations

import re
from pathlib import Path

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import passes, registry
from rebar.llm.review_kernel import decide
from rebar.llm.runner import FakeRunner

CRITERION = "ac-satisfiability"


def _fake_cfg() -> LLMConfig:
    return LLMConfig(model="fake-model")


def test_routing_entry() -> None:
    """Verify advisory coherence routing through the single-turn execution tier.

    Fixed-size facet chunks avoid the per-plan agent budget and add a call only when
    the applicable population fills a chunk. Promotion requires replay calibration.
    """
    assert registry.validate_packaged_routing() == []
    assert CRITERION in registry.CANONICAL_LLM
    crit = registry.by_id()[CRITERION]
    assert registry.exec_tier(crit) == "1-TURN"
    assert crit["facet"] == "coherence"
    assert crit["default_posture"] == "advisory"
    # Advisory criteria sit at the 0.95 default so they cannot block on their own.
    assert crit["block_threshold"] == 0.95
    # It must not be treated as a triggered overlay (those need the Txx id pattern).
    assert not registry.is_overlay(CRITERION)


def test_suppressed_for_types_without_acceptance_criteria() -> None:
    """A bug records evidence and a session_log records history; neither carries the
    acceptance criteria this criterion reasons over, so it would be vacuous there."""
    suppressed = registry.by_id()[CRITERION]["applies_at"]["suppress_types"]
    assert "bug" in suppressed and "session_log" in suppressed


def test_routes_at_both_leaf_and_container_scope() -> None:
    """The defect is intra-document, so it applies to any ticket that HAS criteria —
    a leaf and a container alike. An ``applies_at.scope`` restriction would silently
    exempt half of them (the motivating case included both an epic and its children)."""
    assert "scope" not in registry.by_id()[CRITERION]["applies_at"]


def test_rubric_is_generic() -> None:
    """The rubric must describe the DEFECT CLASS, not the episode that motivated it.

    A rubric written around the motivating epic's own subject vocabulary would only ever
    catch that one migration. The generic primitive is set-quantification-meets-carve-out,
    and all three of its shapes must be named.
    """
    text = registry.by_id()[CRITERION]["scenario"]
    lowered = text.lower()
    # The primitive: an AC quantifies over a set; something else excepts a member.
    assert "quantif" in lowered
    for shape in ("universal", "carve-out", "derived", "cardinality"):
        assert shape in lowered, f"rubric does not name the {shape!r} shape"
    # Genericity guard: the rubric must not name the retired vocabulary whose migration
    # motivated it, nor the epic/bug ids involved — those would date it to one episode.
    assert not re.search(r"success[ _-]criteri(?:a|on)", lowered)
    for episode in ("1386", "elkhound", "2aa6", "35bc"):
        assert episode not in lowered, f"rubric is dated to the {episode!r} episode"
    # It must disclaim the neighbouring owners so the facet boundary survives contact.
    for neighbour in ("COH", "E1", "G3", "G7"):
        assert neighbour in text, f"rubric does not disclaim {neighbour}'s territory"


def test_checklist_covers_each_shape() -> None:
    """Pass-2 answers the checklist keys, so every shape needs its own key — a shape
    folded into prose alone is not independently verifiable."""
    keys = {item["key"] for item in registry.by_id()[CRITERION]["checklist"]}
    assert {
        "jointly_satisfiable",
        "universal_vs_carve_out",
        "derived_artifact_closure",
        "snapshot_cardinality",
        "quoted_intra_document",
    } <= keys


def test_explain_returns_section() -> None:
    """``rebar explain ac-satisfiability`` (and the MCP/library wrappers, which share this
    one lookup) resolve the generated guide section, and the generated guide is in parity
    with the registry — the same regenerate-in-place contract as reviewers/index.json.

    The checkout root is passed explicitly: the guide is resolved relative to a repo root,
    and unit tests run under a sandboxed one.
    """
    checkout = Path(__file__).resolve().parents[2]
    section = registry.explain_criterion(CRITERION, repo_root_path=str(checkout))
    assert section.startswith(f"## {CRITERION}")
    assert registry.validate_criteria_guide(str(checkout)) == []
    guide = (checkout / "docs/plan-review-criteria-guide.md").read_text(encoding="utf-8")
    assert f"## {CRITERION}" in guide


def test_graded_by_existing_internal_conflict_axis() -> None:
    """No new verifier vocabulary: Pass-2 already grades "the plan contradicts itself
    (two requirements or sections cannot both hold)". This criterion is a Pass-1 FINDER
    gap only, which is why nothing under review_kernel/ changes."""
    assert "internal_conflict" in decide._PLAN_SEVERITY_AXES
    # It is NOT a hard-override axis, so it scores on the ordinal ladder like any other.
    assert "internal_conflict" not in decide._PLAN_HARD_OVERRIDE_AXES


def test_reaches_pass1_and_attributes() -> None:
    """Verify Pass 1 sends the rubric and preserves findings attributed to its ID.

    Attribution drops unknown criterion IDs, so the surviving finding proves
    registration and execution. Model judgment accuracy remains part of calibration.
    """
    crit = registry.by_id()[CRITERION]
    captured: dict[str, str] = {}

    class _Capturing:
        name = "capturing"

        def preflight(self) -> None:
            pass

        def run(self, req):  # type: ignore[no-untyped-def]
            captured["instructions"] = req.instructions
            return FakeRunner(
                findings=[
                    {
                        "finding": "AC1 demands zero matches where AC6 protects one",
                        "criteria": [CRITERION],
                    }
                ]
            ).run(req)

    plan = (
        "## Acceptance Criteria\n"
        "- [ ] no file under src/ matches P\n"
        "## Out of Scope\n"
        "- src/keeper.py keeps matching P\n"
    )
    out, _usage = passes.pass1_chunk(_Capturing(), _fake_cfg(), plan=plan, chunk=[crit])

    # The rubric was actually presented to the finder.
    assert CRITERION in captured["instructions"]
    assert "quantif" in captured["instructions"].lower()
    # And its finding survived attribution with the criterion id intact.
    assert [f["criteria"] for f in out] == [[CRITERION]]


def test_co_chunks_with_the_other_coherence_criteria() -> None:
    """Facet-sorted slicing puts the coherence criteria adjacent, so the new criterion
    normally shares a chunk with COH/E1 — which is why it costs at most one extra call, and
    why the reviewer sees COH's "not within-section" disclaimer in the same rubric."""
    single = [
        registry.by_id()[cid]
        for cid in ("F1", "E2", "COH", "E1", CRITERION)
        if cid in registry.by_id()
    ]
    chunks = registry.chunk_by_facet(single, model="claude-sonnet-4-6", ticket_size="moderate")
    home = next(ch for ch in chunks if any(c["id"] == CRITERION for c in ch))
    assert {"COH", "E1"} <= {c["id"] for c in home}
