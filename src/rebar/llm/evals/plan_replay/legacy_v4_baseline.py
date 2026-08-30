"""The plan-v4 ``impact_plan`` formula and its grade->kind classifier, relocated verbatim
from ``docs/calibration/plan_v5_rescore.py`` (ticket bouncy-peacockish-titmouse /
5d19-52e0-7c26-47fb) so Tier-0's replay harness can import them — ``docs/calibration/``
carries no ``__init__.py`` and is not on the import path.

``docs/calibration/plan_v5_rescore.py`` imports every name here instead of defining them
locally; its own behavior is unchanged.
"""

from __future__ import annotations

import re

from rebar.llm.review_kernel import decide

# ── Documented grade -> kind mapping rules ────────────────────────────────────────────
# Ordered; FIRST match wins. Applied to the finding prose + its evidence lines. The rules
# are deliberately GAP-BIASED: a body that describes a genuine gap maps to a FLOORING kind,
# and only a body whose complaint is specificity/framing maps to the advisory kind. The
# residual (no rule matches) is reported separately and scored BOTH ways, so the lost-block
# count is bounded above and below rather than resting on an unstated default.

UNDECOMPOSED_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "missing_required_child",
        re.compile(
            r"not covered by (any|no) sibling|no such sibling|owned-by-none"
            r"|only 1 child|only one child|no corresponding child"
            r"|calls for \d+ child|sibling in the (complete|provided)",
            re.I,
        ),
    ),
    (
        "no_executable_breakdown",
        re.compile(
            r"no implementation steps|zero implementation steps|commits to no design"
            r"|big.bang|without a thin vertical|rather than sequencing a thin"
            r"|riskiest unknown .{0,40}not de-risked|goes directly to a comprehensive",
            re.I,
        ),
    ),
    (
        "bundles_separable_slices",
        re.compile(
            r"bundl|independently[- ](releasable|valuable)|heterogeneous"
            r"|(two|three|four) independent|mixes |joins two|single checkbox"
            r"|completable in one working session",
            re.I,
        ),
    ),
]

DOD_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "certification_cannot_prove",
        re.compile(
            # The plan's stated basis for calling the work done is broken: a named
            # symbol/file/ticket/command does not exist or behaves otherwise, the premise
            # is false, the check is vacuous, or the enumeration the plan calls complete
            # provably omits a site the outcome depends on.
            r"do(es)? not exist|no such |factually wrong|is (factually )?(wrong|incorrect)"
            r"|cannot pass|vacuous|trivially broken|can(not| never) be satisfied"
            r"|satisfied by a trivially|does not (flag|detect|match|check|cover)|is false"
            r"|false premise|cannot be found|hallucinated|contradict|unenumerated"
            r"|omits|omitted|is not listed|silently ignore|unverified claim|incompatible"
            r"|but the plan (deletes|only lists|only names|says)|conflicts with"
            r"|architecturally|does not match the actual|is unverified|would silently"
            # A stated proof that rests on a contradicted fact: the plan claims a property
            # the cited code/ticket does not have, or names a definer that defines nothing.
            r"|but (the|its) (prerequisite|cited|underlying|actual|only) "
            r"|is (explicitly )?documented '?no |does not define|defines no |declares "
            r"|direct consumer|hard-?codes|is a second|treating .{0,40}as a structured",
            re.I,
        ),
    ),
    (
        "uncertifiable_outcome",
        re.compile(
            r"no acceptance criterion|no covering acceptance|no AC\b|has no (dedicated )?"
            r"(acceptance criterion|test|proving)|no test (named|exists|asserts|covers)"
            r"|no (concrete )?proving command|not covered by (any|no)|no criterion "
            r"(verifies|asserts)|uncovered deliverable|owned-by-none|no verifying criterion"
            r"|cannot be verified|cannot verify|no covering test|have no covering test|no fallback"
            r"|no AC (explicitly )?(verifies|requires|exercises|tests)|no test in"
            r"|is never (defined|enumerated) in the plan|no stated (test|verification)",
            re.I,
        ),
    ),
    (
        "underspecified_certification",
        re.compile(
            r"under-?specified|does not (specify|define|state|enumerate)|without specifying"
            r"|not spelled out|never (defined|specified|enumerated|names)|only as a charter"
            r"|no detail on|procedurally framed|effort/process-focused|leaving .{0,30}to "
            r"executor judgment",
            re.I,
        ),
    ),
]

ADVISORY_KIND = {
    "undecomposed": "bundles_separable_slices",
    "dod_uncertifiable": "underspecified_certification",
}
# Sourced from the SHIPPED module so this re-score cannot drift from what the gate does.
FLOOR_KINDS = {
    "undecomposed": decide._UNDECOMPOSED_FLOOR_GRADES,
    "dod_uncertifiable": decide._DOD_FLOOR_GRADES,
}
RULES = {"undecomposed": UNDECOMPOSED_RULES, "dod_uncertifiable": DOD_RULES}


def classify(axis: str, text: str) -> str | None:
    """The kind this recorded ordinal grading maps to, or None when no rule matches."""
    for kind, pat in RULES[axis]:
        if pat.search(text):
            return kind
    return None


def legacy_impact_plan(attrs: dict) -> float:
    """plan-v4 `impact_plan`, reproduced explicitly.

    The BASELINE must not be taken from the shipped `decide.impact_plan`: once the plan-v5
    conversion lands, that function scores the corpus's ordinal grades as 0.0, which silently
    collapses the baseline and makes the comparison report `lost=0` no matter what. Pinning the
    old formula here keeps old-vs-new meaningful from any checkout.
    """
    ordinal_override = ("dod_uncertifiable", "undecomposed")
    contribs = [
        decide._SEV01.get(attrs.get(a), 0.0)
        for a in decide._PLAN_SEVERITY_AXES
        if a not in ("ac_unverifiable", "divergent_implementation")
    ]
    contribs.append(decide.ORACLE_GRADE01.get(attrs.get("ac_unverifiable"), 0.0))
    contribs.append(decide.DIVERGENCE_GRADE01.get(attrs.get("divergent_implementation"), 0.0))
    sev = max(contribs) if contribs else 0.0
    mult = 0.8 if attrs.get("silent_vs_self_revealing") == "self_revealing" else 1.0
    if decide._SEV01.get(attrs.get("dod_uncertifiable"), 0.0) > 0.0:
        mult = 1.0
    amplified = min(1.0, sev * mult)
    override = (
        any(decide._SEV01.get(attrs.get(a), 0.0) > 0.0 for a in ordinal_override)
        or attrs.get("ac_unverifiable") in decide._ORACLE_FLOOR_GRADES
        or attrs.get("divergent_implementation") in decide._DIVERGENCE_FLOOR_GRADES
    )
    result = max(amplified, decide._PLAN_HARD_OVERRIDE_FLOOR) if override else amplified
    return round(result, 4)
