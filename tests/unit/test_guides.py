"""Ticket 34c9: the plan-writing guide documents the Pass-4 coaching moves.

The guide's "Responding to coaching moves" section names the top-five field-frequency moves
with a worked before/after example each, documents the `[operator-attested]` AC tag under the
"state attestation evidence" entry, and indexes every remaining registered move — with every
name matching the `MOVE_REGISTRY` spelling exactly (read from the packaged `rebar._guides`
resource, so an installed rebar serves it from any working directory).
"""

from __future__ import annotations

import re
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from rebar.llm.plan_review.coach_moves import MOVE_REGISTRY
from rebar.llm.plan_review.registry import CANONICAL_LLM, explain_criterion, explain_guide

pytestmark = pytest.mark.unit

SECTION = "## Responding to coaching moves"

# Top five moves by field frequency (spec-by-example 150, plan-the-verification 96,
# riskiest-assumption 51, state-attestation 41, propagate-to-children 22) — spelled
# exactly as MOVE_REGISTRY registers them.
TOP_FIVE = [
    "specification by example",
    "plan the verification",
    "riskiest-assumption test",
    "state attestation evidence",
    "propagate to children",
]


def _guide_text() -> str:
    base = resources.files("rebar._guides")
    return (base / "writing-a-passing-plan.md").read_text(encoding="utf-8")


def _section_text() -> str:
    text = _guide_text()
    assert SECTION in text, "guide is missing the coaching-moves section"
    body = text.split(SECTION, 1)[1]
    # Section runs until the next same-level (##) heading, if any.
    for line in body.splitlines():
        if line.startswith("## ") and not line.startswith("###"):
            body = body.split("\n" + line, 1)[0]
            break
    return body


def test_top_five_names_match_registry_spelling() -> None:
    registered = {m["name"] for m in MOVE_REGISTRY.values()}
    for name in TOP_FIVE:
        assert name in registered, f"test fixture out of sync with MOVE_REGISTRY: {name!r}"


def test_section_names_top_five_in_frequency_order_with_examples() -> None:
    body = _section_text()
    positions = []
    for name in TOP_FIVE:
        heading = f"### {name}"
        assert heading in body, f"missing entry for move {name!r}"
        positions.append(body.index(heading))
    assert positions == sorted(positions), "moves are not in field-frequency order"
    # Every top-five entry carries a before/after example.
    for name, start in zip(TOP_FIVE, positions, strict=True):
        end = min((p for p in positions if p > start), default=len(body))
        entry = body[start:end]
        assert "Before" in entry and "After" in entry, f"move {name!r} lacks a before/after example"


def test_non_codebase_tag_documented() -> None:
    body = _section_text()
    start = body.index("### state attestation evidence")
    end = body.index("### propagate to children")
    assert "[non-codebase]" in body[start:end], (
        "the [non-codebase] AC tag is not documented in the state-attestation entry"
    )


def test_legacy_alias_stays_undocumented() -> None:
    """ADR 0101 keeps `[operator-attested]` ACCEPTED but UNDOCUMENTED. That is only real if
    something enforces it: without this test the legacy spelling drifts back into the guide
    an author reads, and the rename quietly un-does itself."""
    assert "operator-attested" not in _guide_text(), (
        "the legacy alias reappeared in the packaged author guide; it is deliberately "
        "undocumented (ADR 0101) even though the matcher still accepts it"
    )


def test_remaining_registered_moves_indexed() -> None:
    body = _section_text()
    remaining = [m["name"] for m in MOVE_REGISTRY.values() if m["name"] not in TOP_FIVE]
    assert remaining, "registry unexpectedly has no non-top-five moves"
    for name in remaining:
        assert name in body, f"remaining move {name!r} missing from the one-line index"


def test_advisory_section_cross_links_to_moves() -> None:
    text = _guide_text()
    advisory = text.split("## Advisories worth heeding", 1)[1]
    advisory = advisory.split("\n## ", 1)[0]
    assert "#responding-to-coaching-moves" in advisory, (
        "advisory section does not cross-link the coaching-moves section"
    )


def test_t15_derisk_guidance_is_rendered_by_explain_plan() -> None:
    """The supported author-facing surface carries the complete fast-proof contract."""
    guide = explain_guide("plan")
    assert "T15" in guide
    assert "slow codified delivery loop" in guide
    assert "local run or a manual probe against the real target" in guide
    assert "only the resources it created" in guide


def _criterion_bullet(guide: str, criterion: str) -> str:
    match = re.search(
        rf"(?ms)^- \*\*[^\n]*\(`{re.escape(criterion)}`\)\.\*\*.*?(?=^- \*\*|\Z)",
        guide,
    )
    assert match is not None, f"guide is missing its {criterion} bullet"
    return re.sub(r"\s+", " ", match.group())


def test_t15_guidance_is_stack_agnostic_and_scopes_throwaway_cleanup() -> None:
    guidance = _criterion_bullet(explain_guide("plan"), "T15")
    assert "before codifying" in guidance
    assert "only the resources it created" in guidance
    assert "Terraform" not in guidance
    assert "Docker" not in guidance


def test_rebar_explain_plan_cli_renders_t15_guidance() -> None:
    completed = subprocess.run(
        ["rebar", "explain", "plan"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "T15" in completed.stdout
    assert "local run or a manual probe against the real target" in completed.stdout
    assert "only the resources it created" in completed.stdout


def test_rebar_explain_plan_cli_explains_out_of_loop_proof_move() -> None:
    completed = subprocess.run(
        ["rebar", "explain", "plan"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    rendered = re.sub(r"\s+", " ", completed.stdout)
    assert "out-of-loop proof" in rendered
    assert "planning-time spike" in rendered
    assert "execution step" in rendered
    assert "against the real target" in rendered
    assert "before it enters the slow delivery loop" in rendered


def test_hand_authored_docs_name_current_overlay_range() -> None:
    repo_root = Path(__file__).parents[2]
    gate_doc = re.sub(
        r"\s+",
        " ",
        (repo_root / "docs" / "plan-review-gate.md").read_text(encoding="utf-8"),
    )
    registry_doc = re.sub(
        r"\s+",
        " ",
        (repo_root / "src" / "rebar" / "llm" / "plan_review" / "registry.py").read_text(
            encoding="utf-8"
        ),
    )
    # The hand-authored docs name the OVERLAY RANGE (Txx) but deliberately NOT a criterion
    # COUNT: a hard-coded total goes stale on every criterion addition and carries minimal
    # value, so it is not pinned here.
    overlay_numbers = [
        int(match.group(1))
        for criterion in CANONICAL_LLM
        if (match := re.fullmatch(r"T(\d+)[a-z]?", criterion))
    ]
    expected_range = f"T{min(overlay_numbers)}–T{max(overlay_numbers)}"

    for surface, text in (("gate docs", gate_doc), ("registry docstring", registry_doc)):
        documented_range = re.search(r"\b(T\d+–T\d+)(?: triggered)? overlays\b", text)
        assert documented_range is not None, f"{surface} is missing the overlay range"
        assert documented_range.group(1) == expected_range


# --- Ticket 828a: the author guide must not contradict G6's anti-priming rule ---------------
#
# G6 clause (4) is the authoritative contract for what a plan's approach section must contain:
# a POSITIVE rationale for the chosen approach, and explicitly *not* a rejected-alternatives
# section, "that primes implementers with rejected behavior". Clause (3) reinforces it — the
# reviewer generates alternatives, discards them, and "never write[s] them into the plan", so
# "the implementer's plan still contains only ONE approach".
#
# The guide (served by `rebar explain plan`) and the criterion (served by `rebar explain G6`)
# are two independently-canonical artifacts with no generator linking them, so these tests
# assert parity across the same two public seams the CLI uses. They are keyed to the criterion
# text, so they self-invalidate rather than silently ossify if the contract ever changes.

ANTI_PRIMING_CLAUSE = "do NOT require a rejected-alternatives section"

# Author-facing directives that tell a planner to persist a rejected option in the plan.
REJECTED_ALTERNATIVE_DIRECTIVES = [
    "alternative you rejected",
    "rejected alternative is named",
    "\nRejected:",
]


def _g6_text() -> str:
    return explain_criterion("G6")


def test_g6_still_forbids_requiring_rejected_alternatives() -> None:
    """Precondition: the authoritative contract still carries the anti-priming rule.

    If this fails the contract itself changed and the parity tests below are moot — fix the
    contract-derived expectations deliberately rather than deleting the parity tests.
    """
    assert ANTI_PRIMING_CLAUSE in _g6_text(), (
        "G6 no longer carries the anti-priming clause; the guide-parity expectation is stale"
    )


def test_guide_does_not_instruct_authors_to_name_a_rejected_alternative() -> None:
    """`rebar explain plan` must not teach what `rebar explain G6` forbids."""
    assert ANTI_PRIMING_CLAUSE in _g6_text(), "precondition: G6 anti-priming clause present"
    guide = explain_guide("plan")
    offending = [d for d in REJECTED_ALTERNATIVE_DIRECTIVES if d in guide]
    assert not offending, (
        f"the plan guide instructs authors to persist a rejected alternative {offending!r}, "
        f"contradicting G6's {ANTI_PRIMING_CLAUSE!r} (anti-priming)"
    )


def test_worked_example_models_a_single_approach() -> None:
    """The minimum-viable plan example must model one approach, not a chosen/rejected pair."""
    guide = explain_guide("plan")
    # The example is a fenced markdown block, and its own "## " headings are part of the
    # sample plan — so bound it by the fence, not by the next document heading.
    after_heading = guide.split("## A minimum-viable passing plan", 1)[1]
    example = after_heading.split("```markdown", 1)[1].split("```", 1)[0]
    assert "## Approach" in example, "the worked example lost its Approach section"
    assert "Rejected" not in example, (
        "the worked example models a rejected alternative, priming implementers with "
        "rejected behavior (G6 clause 3: the plan contains only ONE approach)"
    )


def test_guide_still_requires_a_positive_rationale() -> None:
    """Negative control: the fix must reframe to G6's positive rationale, not just delete."""
    guide = explain_guide("plan")
    approach_bullet = next(
        line for line in guide.splitlines() if line.startswith("- **`## Approach`**")
    )
    assert "rationale" in approach_bullet.lower() or "why" in approach_bullet.lower(), (
        "the Approach template bullet no longer asks for a rationale; G6 clause (4) makes a "
        "missing positive rationale a finding"
    )
    assert "(G6)" in approach_bullet, "the Approach bullet lost its G6 criterion citation"


def test_weigh_alternatives_coaching_move_is_unchanged() -> None:
    """Negative control: reviewer-side coaching is preserved — G6 only bars persisting losers."""
    body = _section_text()
    assert "weigh alternatives" in body, (
        "the reviewer-side 'weigh alternatives' coaching move must remain documented; "
        "G6 bars persisting a rejected option in the plan, not weighing alternatives"
    )
