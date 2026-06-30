"""The SEPARATE Pass-2 novelty sub-call (child 150b).

Novelty is scored by a DISTINCT sub-call that ALONE receives the prior-review findings and
answers factual matches-prior sub-answers; ``novelty = 1 − mean(matches-prior)``. These tests
pin: the arithmetic + its `NOVELTY_SUBANSWERS` anchor, the structural independence (a distinct
registered contract; the prior findings reach only the novelty seam), and the FAIL-SAFE — any
sub-call error / malformed output degrades a finding to novelty 0.0 (carryover → never dropped).
"""

from __future__ import annotations

import pytest

from rebar.llm import contracts
from rebar.llm.review_kernel import decide, verify

pytestmark = pytest.mark.unit


# ── the scoring arithmetic (novelty = 1 − mean(answerable matches-prior)) ─────────────────────
def test_novelty_arithmetic_and_subanswer_anchor() -> None:
    assert decide.NOVELTY_SUBANSWERS == (
        "restates_prior_defect",
        "cites_prior_location",
        "matches_prior_fix",
    )
    # all "no" → no prior match → fully novel
    assert decide.novelty(dict.fromkeys(decide.NOVELTY_SUBANSWERS, "no")) == 1.0
    # all "yes" → full carryover → zero novelty
    assert decide.novelty(dict.fromkeys(decide.NOVELTY_SUBANSWERS, "yes")) == 0.0
    # mixed: yes/no/insufficient → carryover_match = mean(1,0,.5)=.5 → novelty .5
    assert (
        decide.novelty(
            {
                "restates_prior_defect": "yes",
                "cites_prior_location": "no",
                "matches_prior_fix": "insufficient",
            }
        )
        == 0.5
    )
    # only-answerable subset is averaged; unanswerable/garbage keys skipped
    assert decide.novelty({"restates_prior_defect": "no", "cites_prior_location": "??"}) == 1.0


def test_novelty_failsafe_empty_is_carryover() -> None:
    """No answerable sub-answer → novelty 0.0 (carryover, never dropped) — the safe direction."""
    assert decide.novelty({}) == 0.0
    assert decide.novelty({"restates_prior_defect": "garbage"}) == 0.0


# ── structural independence: a distinct contract, prior findings only on the novelty seam ─────
def test_novelty_contract_registered_distinctly() -> None:
    nov = contracts.response_model_for("novelty")
    ver = contracts.response_model_for("verification")
    assert nov is not ver
    # the plan-review alias resolves to the same novelty shape
    assert contracts.response_model_for("plan_review_novelty").__name__ == nov.__name__
    # the novelty model carries the matches-prior sub-answers + matched_prior_id, NOT severity
    inst = nov(novelties=[{"index": 0}])
    item = inst.novelties[0]
    assert item.matched_prior_id == ""  # default: no match
    for q in decide.NOVELTY_SUBANSWERS:
        assert getattr(item.matches_prior, q) == "insufficient"  # neutral default


def test_prior_findings_only_reach_the_novelty_seam() -> None:
    """The verification listing/instructions never carry prior findings; the novelty context does
    — independence by construction, not by prompt assertion."""
    cur = [{"finding": "current defect", "criteria": ["E2"], "evidence": [], "impact": ""}]
    prior = [
        {"id": "fprev", "finding": "the prior defect", "location": "Scope", "criteria": ["E2"]}
    ]
    verify_text = verify.verify_instructions(list(enumerate(cur)))
    assert "prior defect" not in verify_text
    novelty_ctx = verify.prior_findings_block(prior)
    assert "fprev" in novelty_ctx and "the prior defect" in novelty_ctx


# ── score_novelty end-to-end over an injected seam (FakeRunner-style) ─────────────────────────
_FINDINGS = [
    {"finding": "f0", "criteria": ["E2"], "evidence": [], "impact": ""},
    {"finding": "f1", "criteria": ["T4"], "evidence": [], "impact": ""},
]
_PRIOR = [{"id": "p0", "finding": "prior f0", "location": "Scope", "criteria": ["E2"]}]


def _run(window: int = 100_000):
    return dict(window_tokens=window, est_tokens=len)


def test_score_novelty_happy_path() -> None:
    """A sub-call returning matches-prior sub-answers maps to the right per-index novelty."""

    def run_chunk(instructions: str, context: str):
        # f0 is a carryover (matches prior p0), f1 is novel (no match)
        return [
            {
                "index": 0,
                "matched_prior_id": "p0",
                "matches_prior": {
                    "restates_prior_defect": "yes",
                    "cites_prior_location": "yes",
                    "matches_prior_fix": "yes",
                },
            },
            {
                "index": 1,
                "matched_prior_id": "",
                "matches_prior": {
                    "restates_prior_defect": "no",
                    "cites_prior_location": "no",
                    "matches_prior_fix": "no",
                },
            },
        ]

    got = verify.score_novelty(_FINDINGS, prior_findings=_PRIOR, run_chunk=run_chunk, **_run())
    assert got == {0: 0.0, 1: 1.0}  # carryover → 0.0, novel → 1.0


def test_score_novelty_failsafe_on_raise() -> None:
    """A sub-call that RAISES (timeout/contract/network) → every finding degrades to 0.0."""

    def boom(instructions: str, context: str):
        raise RuntimeError("verifier turn failed")

    got = verify.score_novelty(_FINDINGS, prior_findings=_PRIOR, run_chunk=boom, **_run())
    assert got == {0: 0.0, 1: 0.0}  # carryover fail-safe, never dropped


def test_score_novelty_failsafe_on_malformed() -> None:
    """A sub-call returning malformed/partial output → those findings degrade to 0.0."""

    def junk(instructions: str, context: str):
        return [{"no_index_here": True}, {"index": 0}]  # missing index; missing matches_prior

    got = verify.score_novelty(_FINDINGS, prior_findings=_PRIOR, run_chunk=junk, **_run())
    assert got == {0: 0.0, 1: 0.0}


def test_score_novelty_no_prior_findings_scores_nothing() -> None:
    """With no prior findings (or no findings) there is nothing to match → {} (cc5b treats every
    finding as carryover); the sub-call is not even invoked."""
    calls = []

    def run_chunk(instructions: str, context: str):
        calls.append(1)
        return []

    assert verify.score_novelty(_FINDINGS, prior_findings=[], run_chunk=run_chunk, **_run()) == {}
    assert verify.score_novelty([], prior_findings=_PRIOR, run_chunk=run_chunk, **_run()) == {}
    assert not calls


# ── the discriminates_novelty eval scorer + its extractor ─────────────────────────────────────
def test_discriminates_novelty_scorer() -> None:
    from rebar.llm import eval_scorers as es

    assert "high_novelty" in es.ALLOWED_EXPECTS and "low_novelty" in es.ALLOWED_EXPECTS
    # numeric novelty in output
    novel = es.score("discriminates_novelty", {"expect": "high_novelty"}, {"novelty": 0.8})
    assert novel.applicable and novel.passed
    carry = es.score("discriminates_novelty", {"expect": "low_novelty"}, {"novelty": 0.2})
    assert carry.applicable and carry.passed
    # computed from matches-prior sub-answers (scorer and gate share decide.novelty)
    from_subanswers = es.score(
        "discriminates_novelty",
        {"expect": "low_novelty"},
        {"matches_prior": dict.fromkeys(decide.NOVELTY_SUBANSWERS, "yes")},
    )
    assert from_subanswers.applicable and from_subanswers.passed  # all-yes → novelty 0 → < 0.5
    # a non-novelty case is not applicable
    assert not es.score("discriminates_novelty", {"expect": "high_impact"}, {}).applicable
    # contradiction fails
    bad = es.score("discriminates_novelty", {"expect": "high_novelty"}, {"novelty": 0.1})
    assert bad.applicable and not bad.passed
