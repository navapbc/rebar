"""HELD-OUT registration oracle for the built-in T15 "overlay-derisk" criterion

Unlike `project.measurement-provenance` (story f161, which rides the `.rebar/` project overlay),
T15 ships in the DEFAULT criteria set — so it changes behaviour for every rebar client and its
registration must be complete and self-consistent.

SCOPE BOUNDARY (from the ticket): this pins the criterion ARTIFACT + REGISTRATION + regenerated
guide. Runtime routing BEHAVIOUR — does T15 actually fire on an infra plan and stay silent on an
app-only plan — is proven by the eval-fixtures story 36ab, which depends on this one.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUBRIC = REPO / "src/rebar/llm/reviewers/plan_review_T15.md"
ROUTING = REPO / "src/rebar/llm/plan_review/criteria_routing.json"

# The four checks the rubric body must state, plus the S1-S3 applicability gate and the
# anti-false-positive paragraph.
RUBRIC_REQUIRED = (
    "S1",
    "S2",
    "S3",
    "RISK NAMED",
    "FAST OUT-OF-LOOP PROOF",
    "PROVE-THEN-CODIFY",
    "SCOPED CLEANUP",
    "ANTI-FP",
)


def test_routing_entry_does_not_pin_a_posture() -> None:
    """Advisory-vs-blocking is the registry default's call, explicitly out of scope here."""
    entry = json.loads(ROUTING.read_text())["T15"]
    assert "default_posture" not in entry, "posture must be inherited, not pinned by this story"


def test_t15_is_registered_as_a_canonical_llm_criterion() -> None:
    from rebar.llm.plan_review.registry import CANONICAL_LLM

    assert "T15" in CANONICAL_LLM


def test_registry_validators_pass() -> None:
    """The registration must be self-consistent: full coverage, no routing orphan, and the
    generated guide in step with the registry.

    Pass the repo root EXPLICITLY. An autouse conftest fixture points `REBAR_ROOT` at a
    throwaway sandbox repo for the whole suite, so a validator left to discover its own root
    reports the guide as missing when it is actually present.
    """
    from rebar.llm.plan_review.registry import (
        check_registry_coverage,
        validate_criteria_guide,
        validate_packaged_routing,
    )

    ok, missing = check_registry_coverage()
    assert ok, f"registry coverage gap: {missing}"
    assert not validate_packaged_routing()
    assert not validate_criteria_guide(str(REPO))


def test_descriptor_loads_with_a_descriptive_name() -> None:
    """End-to-end loadability through the real production path."""
    from rebar.llm.plan_review import registry

    by_id = {d.get("id"): d for d in registry.load_criteria(str(REPO))}
    assert "T15" in by_id, f"T15 did not load; got {sorted(by_id)}"
    assert by_id["T15"].get("exec") == "AGENT"
    name = by_id["T15"].get("name")
    assert name and name != "T15", "descriptor name degraded to the bare id (missing title:)"


def test_regenerated_guide_carries_the_full_t15_section() -> None:
    guide = (REPO / "docs/plan-review-criteria-guide.md").read_text()
    assert "## T15" in guide
    for marker in ("RISK NAMED", "FAST OUT-OF-LOOP PROOF", "PROVE-THEN-CODIFY", "SCOPED CLEANUP"):
        assert marker in guide, f"regenerated guide missing {marker!r}"
