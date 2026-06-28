"""GATED LIVE eval: does a real verifier OBEY the kernel's soft rules? (epic vivid-gang-day WS4)

This is the live counterpart to the deterministic ``tests/unit/test_review_kernel_rules.py``.
It runs the SHARED ``verify_findings`` Pass-2 against a LIVE model over a small fixed fixture
and checks the rules behaviorally:

* allow-insufficient — a finding with NO grounding should not be fabricated into a confident
  yes/no; an honest verifier answers ``insufficient`` for the unanswerable sub-questions;
* independence — the verdict does not collapse to the finding's asserted conclusion (a
  deliberately FALSE claim should not be uniformly affirmed).

Marked ``external`` (excluded from the default + the blocking CI path; needs
REBAR_RUN_EXTERNAL=1) and SKIPS unless an API key + the ``agents`` extra are present.
MULTI-RUN with a LENIENT threshold (majority of N) — informational, never a hard gate
(threshold calibration is deferred per the epic). Run locally::

    REBAR_RUN_EXTERNAL=1 ANTHROPIC_API_KEY=… pytest -m external \
        tests/external/test_review_kernel_rules_live.py
"""

from __future__ import annotations

import importlib
import os

import pytest

from rebar.llm import review_kernel

pytestmark = pytest.mark.external

kverify = importlib.import_module("rebar.llm.review_kernel.verify")

_RUNS = 3  # multi-run
_LENIENT_MAJORITY = 2  # ≥2/3 runs must obey — lenient, informational


def _have_live_model() -> bool:
    try:
        import rebar.llm as llm
    except ImportError:
        return False
    if not llm.agents_extra_installed():
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_skip = pytest.mark.skipif(not _have_live_model(), reason="no ANTHROPIC_API_KEY / agents extra")

# A deliberately UNGROUNDED, FALSE claim: the plan context contradicts it. An honest,
# independent verifier should NOT uniformly affirm it (independence) and should answer
# 'insufficient' where the evidence does not decide (allow-insufficient).
_PLAN_CONTEXT = (
    "## What\nReturn HTTP 404 from `get.py` when a record id is absent.\n"
    "## Acceptance Criteria\n- [ ] missing id → 404\n- [ ] a unit test covers the missing-id path\n"
)
_FALSE_FINDING = {
    "finding": "The plan has no acceptance criteria and no test coverage at all.",
    "criteria": ["E1"],
    "evidence": [],
    "impact": "untestable",
}


def _run_chunk_factory():
    """A live per-chunk verifier seam: run the kernel verification prompt over one chunk via the
    real runner. Returns the chunk's ``verifications`` list."""
    from rebar.llm import prompts
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import RunRequest, get_runner

    cfg = LLMConfig.from_env()
    runner = get_runner(cfg)
    prompt = prompts.get_prompt("plan-review-verifier", repo_root=cfg.repo_path)

    def run_chunk(instructions: str, context: str) -> list[dict]:
        system, _meta = prompts.resolve_prompt(prompt, {"plan": context}, repo_root=cfg.repo_path)
        req = RunRequest(
            system_prompt=prompts.strip_volatile_marker(system),
            instructions=instructions,
            config=cfg,
            reviewers=["plan-reviewer"],
            mode="structured",
            output_schema="plan_review_verification",
            execution_mode="single_turn",
        )
        return runner.run(req).get("verifications", []) or []

    return run_chunk


@_skip
def test_live_verifier_obeys_independence_and_allows_insufficient() -> None:
    run_chunk = _run_chunk_factory()
    independent_runs = 0  # the false claim was NOT uniformly affirmed
    honest_runs = 0  # at least one 'insufficient' OR a 'no' (not all fabricated 'yes')
    for _ in range(_RUNS):
        result = kverify.verify_findings(
            [_FALSE_FINDING],
            context=_PLAN_CONTEXT,
            run_chunk=run_chunk,
            window_tokens=1_000_000,
            est_tokens=lambda s: len(s) // 4,
        )
        verif = result["verifications"].get(0)
        if not verif:
            continue  # degraded turn — neither obeyed nor violated; skip this run
        answers = [verif["binary"].get(q) for q in review_kernel.GRADED_BINARY]
        # independence: a false claim should not be graded as fully valid
        if review_kernel.validity(verif["binary"]) < 1.0:
            independent_runs += 1
        # allow-insufficient: an honest verifier uses 'insufficient'/'no', not all-fabricated 'yes'
        if any(a in ("insufficient", "no") for a in answers):
            honest_runs += 1
    assert independent_runs >= _LENIENT_MAJORITY, (
        f"independence: only {independent_runs}/{_RUNS} runs declined to affirm the false claim"
    )
    assert honest_runs >= _LENIENT_MAJORITY, (
        f"allow-insufficient: only {honest_runs}/{_RUNS} runs answered honestly (insufficient/no)"
    )
