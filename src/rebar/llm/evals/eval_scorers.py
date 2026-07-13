"""Deterministic eval scorers — the executable registry behind the scorer NAMES
in the packaged eval specs (epic 6f2d / WS-EVAL-EXISTING).

Until this module existed, a spec's ``deterministic`` scorer was an inert NAME:
``validate_scorer`` only checked the name was non-empty and ``run_eval`` was a stub,
so no named scorer ever executed. This module makes the names REAL — each maps to a
PURE function over ``(dataset_case, reviewer_output) -> ScoreResult`` — so the live
harness (``run_eval``) can gate on them and ``validate_eval_spec(strict=True)`` can
reject a spec that names a scorer with no implementation (no more silent typos).

A deterministic scorer is the GATE (llm-judge scorers only report). Each returns a
:class:`ScoreResult` with ``applicable`` (does this scorer's metric apply to this
case?) and, when applicable, ``passed``. Aggregation across cases/epochs (recall,
``at_least(k)``, coverage) is the caller's job — see :mod:`rebar.llm.evals.eval`.

The reviewer ``output`` is one of:
  * a ``review_result`` ``{findings:[...], ...}`` (review_ticket / scan_spec /
    code-quality / the plan-review finders) — "fired" means >=1 finding;
  * a ``completion_verdict`` ``{verdict:"PASS"|"FAIL", findings:[...]}`` (the
    completion-verifier) — "fired" means verdict FAIL;
  * a per-finding verification ``{validity: 0..1}`` / ``{verdict: "valid"|...}``
    (the plan-review verifier) — graded by :func:`_validity`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Dataset-case `expect` vocabulary. finding/fail = "should fire"; pass = "should not
# fire"; high_validity/low_validity = the verifier's discrimination axis.
FIRE_EXPECTS = frozenset({"finding", "fail"})
NOFIRE_EXPECTS = frozenset({"pass"})
VALIDITY_EXPECTS = frozenset({"high_validity", "low_validity"})
# The verifier's IMPACT axis (distinct from the validity axis): a planted high-impact
# finding must grade impact >= the "major" floor and a low-impact one below it, so the
# Pass-3 rising-floor (drop NOVEL low-impact findings on a remediation re-review) rests
# on a verifier whose severity attributes actually discriminate — not just its validity.
IMPACT_EXPECTS = frozenset({"high_impact", "low_impact"})
# The novelty axis (child 150b): a labeled carryover/novel pair tests the SEPARATE Pass-2
# novelty sub-call's discrimination — a `novel` finding must score novelty >= 0.5 (the per-case
# midpoint) and a `carryover` finding < 0.5, so the Pass-3 rising-floor rests on a novelty signal
# that actually separates new findings from reworded prior ones (not one that rates all the same).
NOVELTY_EXPECTS = frozenset({"high_novelty", "low_novelty"})
ALLOWED_EXPECTS = (
    FIRE_EXPECTS | NOFIRE_EXPECTS | VALIDITY_EXPECTS | IMPACT_EXPECTS | NOVELTY_EXPECTS
)


@dataclass(frozen=True)
class ScoreResult:
    """One deterministic scorer's verdict on one case. ``applicable=False`` means the
    scorer's metric does not cover this case (excluded from its denominator), so
    ``passed`` is ignored."""

    applicable: bool
    passed: bool = False
    detail: str = ""


Scorer = Callable[[dict, dict], ScoreResult]

_NA = ScoreResult(applicable=False)


# ── shared predicates ──────────────────────────────────────────────────────────


def _findings(out: dict) -> list:
    return out.get("findings") or [] if isinstance(out, dict) else []


def _fired(out: dict) -> bool:
    """True when the reviewer flagged a defect: a FAIL verdict, or >=1 finding."""
    if not isinstance(out, dict):
        return False
    if "verdict" in out:
        return str(out.get("verdict", "")).strip().upper() == "FAIL"
    return bool(_findings(out))


def _validity(out: dict) -> float | None:
    """Extract a 0..1 graded validity from a verifier output, or None if absent.

    Tolerant of shape: a numeric ``validity``/``graded_validity``/``score``/
    ``confidence``, or a string ``verdict``/``label`` mapped to the poles."""
    if not isinstance(out, dict):
        return None
    for key in ("validity", "graded_validity", "score", "confidence"):
        v = out.get(key)
        if isinstance(v, int | float):
            return float(v)
    for key in ("verdict", "label"):
        s = out.get(key)
        if isinstance(s, str):
            low = s.strip().lower()
            if low in {"valid", "high", "high_validity", "real", "confirmed", "true"}:
                return 1.0
            if low in {"invalid", "low", "low_validity", "false", "dismissed", "refuted"}:
                return 0.0
    return None


def _expects_high_validity(case: dict) -> bool:
    return case.get("expect") == "high_validity" or str(case.get("kind", "")).lower() == "true"


# severity_label: impact >= 0.5 is "major" or above (decide.severity_label). A planted
# high-impact finding must clear this floor; a low-impact one must fall below it.
_IMPACT_MAJOR_FLOOR = 0.5


def _impact(out: dict) -> float | None:
    """Extract a 0..1 IMPACT for the verified finding, or None if absent. Tolerant of
    shape: a numeric ``impact``, or computed from the coarse ``severity_attributes``
    (top-level, nested under ``verification``, or the five attribute keys at top level)
    via the shared Pass-3 ordinal map (:func:`rebar.llm.review_kernel.decide.impact`),
    so the verifier's raw attributes and the gate's own impact math can never diverge."""
    if not isinstance(out, dict):
        return None
    v = out.get("impact")
    if isinstance(v, int | float) and 0.0 <= float(v) <= 1.0:
        return float(v)
    attrs = out.get("severity_attributes")
    if not isinstance(attrs, dict):
        ver = out.get("verification")
        attrs = ver.get("severity_attributes") if isinstance(ver, dict) else None
    if not isinstance(attrs, dict):
        keys = ("prod_impact", "debt_impact", "blast_radius", "likelihood", "reversibility")
        attrs = {k: out[k] for k in keys if k in out} or None
    if isinstance(attrs, dict) and attrs:
        from rebar.llm.review_kernel.decide import impact as _impact_of

        return _impact_of(attrs)
    return None


def _expects_high_impact(case: dict) -> bool:
    return case.get("expect") == "high_impact"


# ── schema / contract scorers (applicable to every case) ───────────────────────


def _schema_review_result(case: dict, out: dict) -> ScoreResult:
    from rebar.llm.findings import FindingsError, validate_result

    try:
        validate_result(out if isinstance(out, dict) else {})
        return ScoreResult(True, True)
    except FindingsError as exc:
        return ScoreResult(True, False, str(exc))


def _schema_verdict(case: dict, out: dict) -> ScoreResult:
    """completion_verdict contract: verdict in {PASS,FAIL}; FAIL<=>findings; every
    FAIL finding carries at least one citation (the source-citation contract)."""
    if not isinstance(out, dict):
        return ScoreResult(True, False, "output is not a dict")
    verdict = str(out.get("verdict", "")).strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        return ScoreResult(True, False, f"verdict {verdict!r} not in PASS/FAIL")
    findings = _findings(out)
    if verdict == "FAIL" and not findings:
        return ScoreResult(True, False, "FAIL verdict with no findings (FAIL<=>findings)")
    if verdict == "PASS" and findings:
        return ScoreResult(True, False, "PASS verdict but findings present (FAIL<=>findings)")
    for f in findings:
        if not (f.get("citations") or []):
            return ScoreResult(True, False, "a FAIL finding lacks a source citation")
    return ScoreResult(True, True)


def _schema_verification(case: dict, out: dict) -> ScoreResult:
    """The verifier must emit a graded validity (so Pass-3 can gate on it)."""
    v = _validity(out)
    if v is None:
        return ScoreResult(True, False, "no graded validity in verifier output")
    if not 0.0 <= v <= 1.0:
        return ScoreResult(True, False, f"validity {v} out of [0,1]")
    return ScoreResult(True, True)


# ── code-review contract scorers (story f93a) ──────────────────────────────────
# The code-review arm (eval_solver) runs an INDIVIDUAL code-review prompt over a case's
# diff and returns its NATIVE structured output — a base/overlay reviewer emits
# ``{findings:[...]}`` (no `verdict`), the Pass-2 verifier emits ``{verifications:[...]}``.
# So a code-review case is scored on FINDINGS presence (recall / no-fire) via `_fired`
# (which already returns `bool(findings)` for a verdict-less output) and never through the
# completion-verifier's FAIL/BLOCK-only path.


def _schema_code_review_findings(case: dict, out: dict) -> ScoreResult:
    """A base/overlay code-review output must be a dict carrying a ``findings`` list — the
    native Pass-1 evidence shape. Applicable to every case (a shape check, not a fire check):
    an EMPTY findings list is still valid shape. No `verdict` is expected here — the arm
    evaluates one prompt, not the full-gate verdict."""
    if not isinstance(out, dict):
        return ScoreResult(True, False, "output is not a dict")
    if not isinstance(out.get("findings"), list):
        return ScoreResult(True, False, "code-review output has no `findings` list")
    return ScoreResult(True, True)


def _verify_emits_verifications(case: dict, out: dict) -> ScoreResult:
    """The Pass-2 verifier must emit a NON-EMPTY ``verifications`` list (one per finding it
    re-grounds). The T8-advisory contract: verify is scored on PRESENCE of verifications, NOT
    on any FAIL/BLOCK verdict — so this gates on the verifier producing a graded answer set,
    never on a polarity it deliberately leaves to Pass-3."""
    if not isinstance(out, dict):
        return ScoreResult(True, False, "output is not a dict")
    verifs = out.get("verifications")
    if isinstance(verifs, list) and verifs:
        return ScoreResult(True, True)
    return ScoreResult(True, False, "verifier emitted no `verifications`")


# ── recall / no-fire scorers (applicable by `expect`) ──────────────────────────


def _recall(case: dict, out: dict) -> ScoreResult:
    """On a case that SHOULD fire (expect finding/fail), the reviewer must fire."""
    if case.get("expect") not in FIRE_EXPECTS:
        return _NA
    fired = _fired(out)
    return ScoreResult(True, fired, "" if fired else "expected a finding/FAIL, got none")


def _no_fire(case: dict, out: dict) -> ScoreResult:
    """On a case that should PASS (expect pass), the reviewer must NOT fire."""
    if case.get("expect") not in NOFIRE_EXPECTS:
        return _NA
    fired = _fired(out)
    return ScoreResult(True, not fired, "false fire on a good case" if fired else "")


def _cites_real_paths(case: dict, out: dict) -> ScoreResult:
    """Every finding cites at least one resolved file path. Citations are resolved
    upstream (findings.resolve_citations downgrades unresolved file citations to
    kind='source'), so a surviving kind='file' citation is a real path:line."""
    if not _fired(out):
        return _NA
    for f in _findings(out):
        cits = f.get("citations") or []
        if not any(c.get("kind") == "file" and c.get("path") for c in cits):
            return ScoreResult(True, False, "a finding lacks a resolved file citation")
    return ScoreResult(True, True)


# ── verifier discrimination scorers ────────────────────────────────────────────


def _discriminates_true_from_false(case: dict, out: dict) -> ScoreResult:
    """A planted TRUE finding must grade high-validity; a planted FALSE one low."""
    if case.get("expect") not in VALIDITY_EXPECTS and "kind" not in case:
        return _NA
    v = _validity(out)
    if v is None:
        return ScoreResult(True, False, "no graded validity to discriminate on")
    want_high = _expects_high_validity(case)
    got_high = v >= 0.5
    ok = got_high == want_high
    return ScoreResult(True, ok, "" if ok else f"validity {v} contradicts expected {want_high}")


def _attributes_criterion(case: dict, out: dict) -> ScoreResult:
    """A fire case tagged with a `criterion` (e.g. the container finder's G3 coverage
    vs G4 interaction) must be attributed to THAT criterion by at least one finding —
    so attribution accuracy can be diffed (the S4/S5 fidelity metric). Applicable only
    to should-fire cases that name a criterion."""
    crit = case.get("criterion")
    if case.get("expect") not in FIRE_EXPECTS or not crit:
        return _NA
    needle = str(crit).lower()
    for f in _findings(out):
        hay = " ".join(str(f.get(k, "")) for k in ("criterion", "dimension", "title", "detail"))
        if needle in hay.lower():
            return ScoreResult(True, True)
    return ScoreResult(True, False, f"no finding attributed to criterion {crit!r}")


def _enumerates_all_unmet_criteria(case: dict, out: dict) -> ScoreResult:
    """A FAIL verdict must enumerate EVERY unmet criterion — one distinct finding per
    unmet criterion (anti-ratchet: never stop at the first failure). Applicable only to
    cases that annotate `expected_unmet_criteria` (a list of short substrings naming the
    criteria that should each be independently flagged). Each expected substring must be
    matched (case-insensitive) by a finding's `criterion` field, and the match must be
    BIJECTIVE — each expected substring is covered by a DISTINCT finding, so one finding
    cannot stand in for two unmet criteria."""
    expected = case.get("expected_unmet_criteria")
    if not expected:
        return _NA
    findings = _findings(out)
    crits = [str(f.get("criterion", "")).lower() for f in findings]
    used: set[int] = set()
    for exp in expected:
        needle = str(exp).lower()
        matched = False
        for i, hay in enumerate(crits):
            if i in used:
                continue
            if needle in hay:
                used.add(i)
                matched = True
                break
        if not matched:
            return ScoreResult(
                True, False, f"no distinct finding enumerates unmet criterion {exp!r}"
            )
    return ScoreResult(True, True)


def _discriminates_impact_levels(case: dict, out: dict) -> ScoreResult:
    """A planted HIGH-impact finding must grade impact >= the "major" floor; a planted
    LOW-impact one below it. This is the IMPACT analogue of
    :func:`_discriminates_true_from_false` (validity): it guards the severity attributes
    the Pass-3 rising-floor relies on, so the verifier cannot silently regress to rating
    every finding the same (the saturation failure the floor would be blind to)."""
    if case.get("expect") not in IMPACT_EXPECTS:
        return _NA
    imp = _impact(out)
    if imp is None:
        return ScoreResult(True, False, "no impact/severity_attributes to discriminate on")
    want_high = _expects_high_impact(case)
    got_high = imp >= _IMPACT_MAJOR_FLOOR
    ok = got_high == want_high
    return ScoreResult(
        True, ok, "" if ok else f"impact {imp:.2f} contradicts expected high={want_high}"
    )


# severity_label analogue for novelty: the per-case 0.5 midpoint a novel finding clears and a
# carryover finding falls below (the framework idiom; NOT an AUC literal). Distinct from the
# runtime drop threshold T_novel (cc5b) — 0.5 is "did the scorer separate novel from carryover".
_NOVELTY_MIDPOINT = 0.5


def _novelty(out: dict) -> float | None:
    """Extract a 0..1 NOVELTY for the scored finding, or None if absent. Tolerant of shape: a
    numeric ``novelty``, else computed from the matches-prior sub-answers (top-level, nested under
    ``matches_prior``, or the three sub-answer keys at top level) via the shared Pass-3 ordinal map
    (:func:`rebar.llm.review_kernel.decide.novelty`), so the scorer and the gate's own novelty math
    can never diverge."""
    if not isinstance(out, dict):
        return None
    v = out.get("novelty")
    if isinstance(v, int | float) and 0.0 <= float(v) <= 1.0:
        return float(v)
    from rebar.llm.review_kernel.decide import NOVELTY_SUBANSWERS
    from rebar.llm.review_kernel.decide import novelty as _novelty_of

    mp = out.get("matches_prior")
    if not isinstance(mp, dict):
        mp = {k: out[k] for k in NOVELTY_SUBANSWERS if k in out} or None
    if isinstance(mp, dict) and any(k in mp for k in NOVELTY_SUBANSWERS):
        return _novelty_of(mp)
    return None


def _expects_high_novelty(case: dict) -> bool:
    return case.get("expect") == "high_novelty" or str(case.get("kind", "")).lower() == "novel"


def _discriminates_novelty(case: dict, out: dict) -> ScoreResult:
    """A labeled NOVEL finding must score novelty >= the 0.5 midpoint; a CARRYOVER one below it.
    The novelty analogue of the validity/impact discriminators
    (``_discriminates_true_from_false`` / ``_discriminates_impact_levels``): it is the evidence
    gate the Pass-3 rising-floor (cc5b) rests on — a novelty signal that cannot
    tell a genuinely new finding from a reworded prior one would make the floor drop real new
    findings, so this must clear its bar before the drop rule is activated."""
    if case.get("expect") not in NOVELTY_EXPECTS:
        return _NA
    nov = _novelty(out)
    if nov is None:
        return ScoreResult(True, False, "no novelty/matches_prior to discriminate on")
    want_high = _expects_high_novelty(case)
    got_high = nov >= _NOVELTY_MIDPOINT
    ok = got_high == want_high
    return ScoreResult(
        True, ok, "" if ok else f"novelty {nov:.2f} contradicts expected high={want_high}"
    )


def _no_sycophancy(case: dict, out: dict) -> ScoreResult:
    """A real defect must not be sycophantically dismissed (graded low)."""
    if not _expects_high_validity(case):
        return _NA
    v = _validity(out)
    if v is None:
        return ScoreResult(True, False, "no graded validity on a real defect")
    ok = v >= 0.5
    return ScoreResult(True, ok, "" if ok else f"real defect dismissed (validity {v})")


# ── the registry ───────────────────────────────────────────────────────────────
# Names are the public contract (they appear in the packaged *.eval.yaml). Several
# names alias one archetype where the semantics are identical (recall is recall
# whether the bad thing is a "seeded defect", an "incomplete impl", or a "coverage
# gap"); the distinct names keep each spec self-documenting.

REGISTRY: dict[str, Scorer] = {
    # schema / contract
    "emits_valid_review_result": _schema_review_result,
    "emits_valid_findings": _schema_review_result,
    "emits_valid_verdict": _schema_verdict,
    "emits_valid_verification": _schema_verification,
    # code-review (story f93a): findings-based, NOT the FAIL/BLOCK verdict path
    "code_review_emits_valid_findings": _schema_code_review_findings,
    "code_review_recall": _recall,
    "code_review_no_fire": _no_fire,
    "code_review_verify_emits_verifications": _verify_emits_verifications,
    # recall (should-fire cases)
    "recall_on_seeded_defects": _recall,
    "recall_on_silent_drop": _recall,
    "recall_on_incomplete": _recall,
    "recall_on_gaps_and_conflicts": _recall,
    "recall_on_uncovered_or_inconsistent": _recall,
    # no-fire (good cases)
    "no_fire_on_good_cases": _no_fire,
    "no_fire_on_honored_or_justified_descope": _no_fire,
    "no_false_fail_on_complete": _no_fire,
    "no_fire_on_aligned": _no_fire,
    "no_fire_on_covered_or_consistent": _no_fire,
    # grounding / attribution
    "cites_real_paths": _cites_real_paths,
    "attributes_g3_vs_g4": _attributes_criterion,
    "enumerates_all_unmet_criteria": _enumerates_all_unmet_criteria,
    # verifier discrimination
    "discriminates_true_from_false": _discriminates_true_from_false,
    "discriminates_impact_levels": _discriminates_impact_levels,
    "discriminates_novelty": _discriminates_novelty,
    "no_sycophancy_on_real_defects": _no_sycophancy,
}


def known_scorer_names() -> frozenset[str]:
    """The registered deterministic-scorer names. ``validate_eval_spec(strict=True)``
    rejects any deterministic scorer whose name is not in this set."""
    return frozenset(REGISTRY)


def score(name: str, case: dict, out: dict) -> ScoreResult:
    """Run one registered deterministic scorer. Raises ``KeyError`` for an unknown
    name (call :func:`known_scorer_names` to validate first)."""
    return REGISTRY[name](case, out)
