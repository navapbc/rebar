"""Overlay-wiring tests for the `project.measurement-provenance` criterion (story f161).

These pin the ACs that the DET-lint tests cannot reach: that the criterion rides the `.rebar/`
PROJECT OVERLAY (never the shipped default set), that it is tool-enabled so its contradiction
probe can actually read the repo, and that the shipped `[operator-attested]` contract text is
left byte-identical so no other rebar client changes behaviour.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CRITERION = "project.measurement-provenance"
ROUTING = REPO / ".rebar" / "criteria_routing.json"
PROMPT = REPO / ".rebar" / "prompts" / "plan-review-project-measurement-provenance.md"

# The four shipped sites that carry the [operator-attested] evidence contract. Editing any of
# them would change the contract for EVERY rebar client — which is exactly what this story
# scoped out. `docs/plan-review-criteria-guide.md` is GENERATED and parity-gated, so it is
# included here too.
SHIPPED_CONTRACT_SITES = (
    "src/rebar/llm/reviewers/plan_review_F1.md",
    "src/rebar/llm/reviewers/plan_review_E2.md",
    "src/rebar/llm/reviewers/plan_review_E6.md",
    "src/rebar/llm/plan_review/coach_moves.py",
)

# `docs/plan-review-criteria-guide.md` is GENERATED from the registry, so it legitimately
# changes whenever ANY criterion is added (e.g. T15). Byte-identity is therefore the wrong
# assertion for it — what must not change is the CONTRACT TEXT it carries.
GENERATED_GUIDE = "docs/plan-review-criteria-guide.md"
CONTRACT_MARKER = "OPERATOR-ATTESTED RULE"


def _routing() -> dict:
    return json.loads(ROUTING.read_text())


def test_criterion_is_defined_and_activated_in_the_overlay() -> None:
    """The criterion must be BOTH defined and activated, at advisory posture and AGENT tier."""
    d = _routing()
    assert CRITERION in d["plan_review"], "criterion not defined in the overlay"
    entry = d["plan_review"][CRITERION]
    assert entry["default_posture"] == "advisory", "ADR-0054: must not ship blocking"
    assert entry["exec"] == "AGENT"
    assert "plan_review" in d["activate"][CRITERION], "criterion defined but never activated"


def test_criterion_has_a_deterministic_trigger() -> None:
    """An AGENT-tier criterion costs ~85x a single-turn call, so it must not route on every
    plan — it fires only on plans that actually carry the [operator-attested] tag."""
    entry = _routing()["plan_review"][CRITERION]
    trigger = entry.get("trigger")
    assert trigger, "AGENT-tier criterion must carry a deterministic trigger"
    blob = json.dumps(trigger)
    assert "operator-attested" in blob


def test_criterion_never_enters_the_shipped_default_set() -> None:
    """AC-6: `grep -rn 'project.measurement-provenance' src/rebar/` must be empty, so no other
    rebar client gains a new criterion from this story."""
    hit = subprocess.run(
        ["grep", "-rn", CRITERION, "src/rebar/"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert hit.returncode != 0, f"criterion leaked into the shipped set:\n{hit.stdout}"


def test_rubric_prompt_exists() -> None:
    """Every activated non-DET project.<name> criterion resolves its rubric body from
    .rebar/prompts/plan-review-project-<name>.md; without it the criterion raises
    PromptNotFound and the whole LLM half of the mechanism silently fails to load."""
    assert PROMPT.is_file(), f"missing rubric prompt: {PROMPT}"


def test_rubric_prompt_is_tool_enabled() -> None:
    """THE load-bearing one. Tooling is granted by the PROMPT's execution_mode, not by the
    routing entry's exec tier — so a prompt left at `single_turn` (as all three sibling project
    prompts are) ships a criterion that CANNOT read infra/terraform/versions.tf, silently
    defeating the environment-contradiction probe."""
    lines = PROMPT.read_text().splitlines()
    assert "execution_mode: agentic" in [ln.strip() for ln in lines], (
        "rubric must declare execution_mode: agentic, else the probe cannot read the repo"
    )


def test_rubric_prompt_has_a_title() -> None:
    """build_descriptor computes `name = prompt.title or cid`, so a missing title degrades the
    criterion's rendered name to the bare id."""
    lines = [ln.strip() for ln in PROMPT.read_text().splitlines()]
    assert any(ln.startswith("title:") and len(ln) > len("title:") + 1 for ln in lines)


def test_rubric_states_all_four_decision_rules() -> None:
    """The rubric is the single place the criterion's decision rules live. All four must be
    present, or a rule referenced by an AC has no mechanism producing it."""
    body = PROMPT.read_text().lower()
    assert "no-anchor" in body or "no comparable" in body, "rule (b) no-anchor fallback missing"
    assert "privilege_posture" in body, "rule (c) privilege-posture judgement missing"
    assert "instrument" in body, "rule (d) instrument-vs-authorization missing"
    assert "environment" in body, "rule (a) environment-vs-anchor missing"


def test_criterion_descriptor_builds_without_prompt_not_found() -> None:
    """The end-to-end loadability guard, through the REAL production path. `load_criteria`
    builds a descriptor for every activated criterion and translates any prompt-load failure
    into a RegistryError — so a missing or unreadable rubric fails HERE rather than silently
    at review time."""
    from rebar.llm.plan_review import registry

    descriptors = registry.load_criteria(str(REPO))
    by_id = {d.get("id"): d for d in descriptors}
    assert CRITERION in by_id, f"{CRITERION} did not load; got {sorted(by_id)}"
    name = by_id[CRITERION].get("name")
    assert name and name != CRITERION, "descriptor name degraded to the bare id (no title:)"


@pytest.mark.parametrize("path", SHIPPED_CONTRACT_SITES)
def test_shipped_contract_text_is_untouched(path: str) -> None:
    """No cross-client behaviour change: the four shipped [operator-attested] contract sites
    (and the generated, parity-gated guide) must be byte-identical to origin/main."""
    r = subprocess.run(
        ["git", "diff", "--exit-code", "origin/main", "--", path],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"{path} was modified — that changes the contract for ALL clients"


def test_generated_guide_still_carries_the_unchanged_contract_text() -> None:
    """The guide is GENERATED, so it changes whenever any criterion is added — byte-identity
    would be a false alarm (and would break the moment T15 lands). What must hold is that the
    OPERATOR-ATTESTED contract SENTENCES it carries are untouched: this story amends the
    contract in an ADR and the project overlay, never in the shipped text."""
    before = subprocess.run(
        ["git", "show", f"origin/main:{GENERATED_GUIDE}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    after = (REPO / GENERATED_GUIDE).read_text()

    def contract_lines(blob: str) -> list[str]:
        return [ln.strip() for ln in blob.splitlines() if CONTRACT_MARKER in ln]

    old, new = contract_lines(before), contract_lines(after)
    assert old, "fixture guard: origin/main's guide should carry the contract text"
    assert new == old, "the [operator-attested] contract text in the generated guide changed"


def test_det_check_has_no_world_measurement_reach() -> None:
    """The rejected alternative stays rejected: the DET module is a pure text check with no
    network or shell reach, so it can never re-run a world measurement."""
    src = (REPO / "src/rebar/llm/plan_review/det_measurement_provenance.py").read_text()
    for forbidden in ("boto3", "botocore", "subprocess", "requests", "urllib", "socket", "httpx"):
        assert forbidden not in src, f"{forbidden} must not appear in the DET check"
