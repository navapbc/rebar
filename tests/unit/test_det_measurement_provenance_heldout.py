"""HELD-OUT oracle for the measurement-provenance DET lint (story f161, epic 3147).

Withheld from the implementation subagent by design: it sees only the happy path in
``test_det_measurement_provenance.py``. These cases separate a real implementation from one
that fakes the happy path — per-key absence, placeholder semantics, enum membership, the
binding rule, the explicit NO-CORRECTNESS-JUDGEMENT boundary, and the advisory wiring that
grandfathers every ticket predating the contract.
"""

from __future__ import annotations

import pytest

from rebar.llm.plan_review.det_floor import PlanContext, p6_ac_quality
from rebar.llm.plan_review.det_measurement_provenance import (
    INSTRUMENTS,
    PRIVILEGE_POSTURES,
    PROVENANCE_KEYS,
    provenance_gaps,
)

_JUST = "— the CI role is the role prod uses, so a missing grant fails exactly as in prod"
_FIELDS = {
    "environment": "896586841071/us-east-1",
    "principal": "arn:aws:iam::896586841071:role/rebar-ci",
    "privilege_posture": "production-equivalent",
    "instrument": "live-call",
}


def _decl(**overrides: str) -> str:
    f = dict(_FIELDS)
    for k, v in overrides.items():
        if v is None:  # type: ignore[comparison-overlap]
            f.pop(k, None)
        else:
            f[k] = v
    body = "; ".join(f"{k}={f[k]}" for k in PROVENANCE_KEYS if k in f)
    return f"provenance: {body} {_JUST}"


def _plan(*lines: str) -> str:
    return "## Acceptance Criteria\n" + "\n".join(lines) + "\n"


def _attested(decl: str | None) -> str:
    head = "- [ ] [operator-attested] the suite passes, evidenced by the Gerrit change id"
    return _plan(head) if decl is None else _plan(head, f"      {decl}")


# ---------------------------------------------------------------- per-key absence


@pytest.mark.parametrize("missing", PROVENANCE_KEYS)
def test_each_missing_key_is_flagged(missing: str) -> None:
    """Every one of the FOUR keys is independently required. `instrument` in particular is
    NOT optional: epic 3147 records that a simulation and a live call are not interchangeable
    evidence, and privilege_posture cannot tell them apart (escape 2b)."""
    f = {k: v for k, v in _FIELDS.items() if k != missing}
    body = "; ".join(f"{k}={v}" for k, v in f.items())
    gaps = provenance_gaps(_attested(f"provenance: {body} {_JUST}"))
    assert len(gaps) == 1, f"missing {missing} must be flagged"
    assert any(missing in r for r in gaps[0][1]), f"the reason must name {missing}: {gaps[0][1]}"


# ---------------------------------------------------------------- placeholder semantics


@pytest.mark.parametrize("bad", ["", "TBD", "todo", "N/A", "?", "-", "<account>", "   "])
def test_placeholder_value_counts_as_absent(bad: str) -> None:
    """A placeholder records the FORM of a declaration without its content. Treated as ABSENT,
    otherwise the contract is satisfiable by typing `TBD` four times."""
    gaps = provenance_gaps(_attested(_decl(environment=bad)))
    assert len(gaps) == 1
    assert any("environment" in r for r in gaps[0][1])


@pytest.mark.parametrize("bad_just", ["", "— TBD", "—", "— <why>"])
def test_placeholder_justification_counts_as_absent(bad_just: str) -> None:
    """The justification is a REQUIRED part of the declaration: a posture recorded without the
    reasoning that makes it reviewable is the 'confidence outrunning method' failure itself."""
    body = "; ".join(f"{k}={v}" for k, v in _FIELDS.items())
    gaps = provenance_gaps(_attested(f"provenance: {body} {bad_just}".strip()))
    assert len(gaps) == 1, f"justification {bad_just!r} must be treated as absent"


# ---------------------------------------------------------------- enum membership


@pytest.mark.parametrize("bad", ["prod-equivalent", "wider", "unknown", "production_equivalent"])
def test_privilege_posture_must_be_an_allowed_literal(bad: str) -> None:
    gaps = provenance_gaps(_attested(_decl(privilege_posture=bad)))
    assert len(gaps) == 1
    assert any("privilege_posture" in r for r in gaps[0][1])


@pytest.mark.parametrize("bad", ["live", "sim", "guess", "static analysis"])
def test_instrument_must_be_an_allowed_literal(bad: str) -> None:
    gaps = provenance_gaps(_attested(_decl(instrument=bad)))
    assert len(gaps) == 1
    assert any("instrument" in r for r in gaps[0][1])


@pytest.mark.parametrize("posture", PRIVILEGE_POSTURES)
def test_every_declared_posture_literal_is_accepted(posture: str) -> None:
    """All three postures are legal DECLARATIONS. Judging whether `broader` is APPROPRIATE is
    the AGENT criterion's rule (c), not the DET floor's business."""
    assert provenance_gaps(_attested(_decl(privilege_posture=posture))) == []


@pytest.mark.parametrize("instrument", INSTRUMENTS)
def test_every_declared_instrument_literal_is_accepted(instrument: str) -> None:
    """Likewise: `instrument=simulation` is a legal declaration. Flagging a simulation offered
    for an AUTHORIZATION claim is rule (d), owned by the AGENT criterion."""
    assert provenance_gaps(_attested(_decl(instrument=instrument))) == []


# ---------------------------------------------------------------- scope + binding


def test_untagged_ac_item_is_never_flagged() -> None:
    """The contract applies ONLY to [operator-attested] items. A normal AC proves itself in the
    codebase and owes no measurement provenance — flagging it would nag every plan."""
    text = _plan("- [ ] the parser is added in `src/rebar/x.py`; proof: `pytest tests/x.py -q`")
    assert provenance_gaps(text) == []


def test_near_miss_tag_is_not_the_tag() -> None:
    """ADR-0043 matches the hyphenated token exactly; [operator_attested] is not it, so the
    item is out of THIS lint's scope (the sibling lint is what nags about the malformed tag)."""
    assert provenance_gaps(_plan("- [ ] [operator_attested] deployed to prod")) == []


def test_declaration_binds_to_the_nearest_preceding_item() -> None:
    """Two attested items, one declaration: it binds to the item directly above it, so the
    OTHER item is the one flagged. Without this rule a single declaration would launder every
    attested AC in the plan."""
    text = _plan(
        "- [ ] [operator-attested] FIRST claim, undeclared",
        "- [ ] [operator-attested] SECOND claim, declared",
        f"      {_decl()}",
    )
    gaps = provenance_gaps(text)
    assert len(gaps) == 1
    assert "FIRST" in gaps[0][0]


def test_detached_declaration_does_not_satisfy_an_item() -> None:
    """A provenance line that is not attached under any checkbox item satisfies nothing."""
    text = _plan(
        "- [ ] [operator-attested] the suite passes",
        "",
        "Some prose paragraph.",
        f"{_decl()}",
    )
    assert len(provenance_gaps(text)) == 1


def test_provenance_key_is_case_insensitive() -> None:
    """`Provenance:` is the same key — casing is not a way to fail the contract silently."""
    assert provenance_gaps(_attested(_decl().replace("provenance:", "Provenance:", 1))) == []


# ------------------------------------------------- the NO-CORRECTNESS-JUDGEMENT boundary


def test_det_makes_no_judgement_about_correctness() -> None:
    """THE boundary that keeps this check deterministic. A declaration naming the WRONG account
    (the real 579718921998-vs-896586841071 escape) is well-FORMED, so the DET floor passes it.
    Catching the contradiction requires reading infra/ — that is the AGENT criterion's rule (a).
    If this ever fails, the DET check has started making semantic judgements it cannot ground."""
    wrong_account = _decl(environment="579718921998/us-east-2")
    assert provenance_gaps(_attested(wrong_account)) == []


def test_det_does_not_judge_instrument_against_the_claim() -> None:
    """Rule (d) (simulation offered for an authorization claim) is LLM work: 'is this an
    authorization claim?' is a reading task no lexicon answers reliably. DET stays out."""
    text = _plan(
        "- [ ] [operator-attested] the scoped IAM grant permits the call",
        f"      {_decl(instrument='simulation')}",
    )
    assert provenance_gaps(text) == []


# ---------------------------------------------------------------- E2E: advisory wiring


def test_p6_surfaces_the_gap_but_never_blocks() -> None:
    """END-TO-END through the real production path. The lint reaches reviewers via
    p6_ac_quality, which is ADVISORY: it may report `fail`, but `blocking` must stay False."""
    ctx = PlanContext(
        ticket_id="t",
        ticket_type="task",
        title="T",
        description=_attested(None),
    )
    r = p6_ac_quality(ctx)
    assert r.blocking is False, "p6 must never block — this is what grandfathers old tickets"
    assert r.finding is not None
    assert any("provenance" in e.lower() for e in r.finding["evidence"])


def test_p6_clean_when_declaration_is_complete() -> None:
    """Contrast case: a correctly-declared plan produces NO provenance finding, so the lint
    does not fire on every well-formed ticket."""
    ctx = PlanContext(
        ticket_id="t",
        ticket_type="task",
        title="T",
        description=_attested(_decl()),
    )
    r = p6_ac_quality(ctx)
    evidence = (r.finding or {}).get("evidence", []) if r.finding else []
    assert not any("provenance" in e.lower() for e in evidence)


def test_pre_contract_ticket_still_claimable_advisory_only() -> None:
    """GRANDFATHERING (the migration-break guard). A ticket predating this contract carries an
    [operator-attested] AC with no provenance at all; it must be COACHED, never blocked, so no
    existing ticket needs a migration step to keep claiming and closing."""
    ctx = PlanContext(
        ticket_id="legacy",
        ticket_type="task",
        title="Legacy",
        description=_plan(
            "- [ ] [operator-attested] the change landed on main through Gerrit, "
            "attested by the recorded change id and its Verified +1",
        ),
    )
    assert p6_ac_quality(ctx).blocking is False
