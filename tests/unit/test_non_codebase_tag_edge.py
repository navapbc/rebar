"""HELD-OUT edge oracle for the `[non-codebase]` tag (story 3726, ADR 0101).

Withheld from the implementation subagent so a passing run means the implementation
generalized rather than fitted the happy path. Covers the near-miss fail-safe, the findall
arity trap, the single-source identity seam, and the two plan-time surfaces the parent AC
claims parity for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rebar.llm.plan_review import decide_ops
from rebar.llm.plan_review.det_operator_attested import (
    _OPERATOR_ATTESTED_TAG_RE,
    ac_item_lines,
    operator_evidence_ac_gaps,
)

REPO = Path(__file__).resolve().parents[2]


def _ac(*items: str) -> str:
    return "Body.\n\n## Acceptance Criteria\n" + "\n".join(items) + "\n"


def _gaps(text: str) -> list[tuple[str, list[str]]]:
    return operator_evidence_ac_gaps(ac_item_lines(text))


@pytest.mark.parametrize(
    "line",
    [
        "- [ ] [NON-CODEBASE] the fix is deployed to prod",
        "- [ ] [Non-Codebase] the fix is deployed to prod",
        "- [x] [non-codebase] the fix is deployed to prod",
    ],
)
def test_case_insensitive_and_checked_box(line: str) -> None:
    """Matching is case-insensitive on the token and indifferent to the box state, exactly as
    it has always been for the legacy spelling."""
    assert _OPERATOR_ATTESTED_TAG_RE.match(line)


@pytest.mark.parametrize(
    "bad",
    [
        "- [ ] [non_codebase] the fix is deployed to prod",
        "- [ ] [noncodebase] the fix is deployed to prod",
        "- [ ] [non codebase] the fix is deployed to prod",
        "- [ ] [operator_attested] the fix is deployed to prod",
        "- [ ] [codebase] the fix is deployed to prod",
    ],
)
def test_near_misses_fail_safe_to_codebase_verifiable(bad: str) -> None:
    """ADR 0043's fail-safe survives ADR 0101: the accepted set grew by exactly ONE token.
    Anything else — including `[codebase]`, which is a real but prompt-level tag — falls back
    to the stricter codebase-verifiable bar, so the operational AC is still flagged."""
    assert _OPERATOR_ATTESTED_TAG_RE.match(bad) is None
    assert _gaps(_ac(bad))


def test_findall_returns_plain_strings_not_tuples() -> None:
    """THE ARITY TRAP. `operator_attested_ac_texts` uses `findall()`, which yields TUPLES the
    moment the alternation captures. The alternation must be NON-capturing, or Pass-3
    enrichment silently breaks on every tagged AC."""
    desc = _ac(
        "- [ ] [non-codebase] deployed to prod",
        "- [ ] [operator-attested] landed on main via Gerrit",
    )
    texts = decide_ops.operator_attested_ac_texts(desc)
    assert len(texts) == 2
    assert all(isinstance(t, str) for t in texts), f"findall returned non-str: {texts!r}"
    assert texts == ["deployed to prod", "landed on main via Gerrit"]


def test_single_source_identity_seam_is_preserved() -> None:
    """decide_ops must expose the SAME compiled object — `==` on a recompiled
    pattern would pass while silently forking the two matchers."""
    assert decide_ops._OPERATOR_ATTESTED_AC_RE is _OPERATOR_ATTESTED_TAG_RE


def test_alternation_is_non_capturing_in_the_pattern_source() -> None:
    """Belt-and-braces on the trap: the pattern itself declares exactly one capturing group
    (the criterion text), so callers relying on group(1)/findall arity stay correct."""
    assert _OPERATOR_ATTESTED_TAG_RE.groups == 1


@pytest.mark.parametrize("tag", ["[non-codebase]", "[operator-attested]"])
def test_plan_time_lint_parity(tag: str) -> None:
    """Parent AC parity surface 1: the p6 advisory lint treats both spellings identically."""
    from rebar.llm.plan_review.det_advisory import p6_ac_quality
    from rebar.llm.plan_review.det_floor import PlanContext

    ctx = PlanContext(
        ticket_id="t",
        ticket_type="task",
        title="T",
        description=_ac(f"- [ ] {tag} the fix is deployed to prod and the gate passes"),
    )
    assert p6_ac_quality(ctx).coverage["operator_attested_gaps"] == 0


@pytest.mark.parametrize("tag", ["[non-codebase]", "[operator-attested]"])
def test_enrichment_parity(tag: str) -> None:
    """Parent AC parity surface 2: Pass-3 enrichment recognizes both spellings."""
    assert decide_ops.operator_attested_ac_texts(_ac(f"- [ ] {tag} deployed to prod")) == [
        "deployed to prod"
    ]


def test_routing_trigger_fires_on_both_spellings() -> None:
    """The `.rebar` overlay TRIGGER gates whether project.measurement-provenance runs at all.
    Un-widened it does not error — it SILENTLY skips the criterion for the new tag."""
    routing = json.loads((REPO / ".rebar/criteria_routing.json").read_text())
    trigger = routing["plan_review"]["project.measurement-provenance"]["trigger"]
    patterns = [p for entry in trigger for p in entry.get("text_all", [])]
    assert patterns, "criterion lost its deterministic trigger"
    for probe in ("- [ ] [non-codebase] x", "- [ ] [operator-attested] x"):
        assert any(re.search(p, probe) for p in patterns), f"trigger does not fire on {probe!r}"
