"""External evaluation of review-kernel independence and insufficient-evidence rules.

The shared ``verify_findings`` second pass evaluates a fixed fixture through the configured
provider. An ungrounded false claim must not be uniformly affirmed. Unanswerable subquestions
may return ``insufficient``.

The test requires the external opt-in, agents package, and provider credential. It runs a lenient
majority threshold for informational calibration rather than a blocking gate.
"""

from __future__ import annotations

import importlib

import _live_llm
import pytest

from rebar.llm import review_kernel

pytestmark = pytest.mark.external

# Auto-marks this module's tests `llm_live` (tests/external/conftest.py).
_live_llm_ready = _live_llm.live_llm_ready()

kverify = importlib.import_module("rebar.llm.review_kernel.verify")

_RUNS = 3  # multi-run
_LENIENT_MAJORITY = 2  # ≥2/3 runs must obey — lenient, informational


_skip = _live_llm.skip_without_live_llm

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
    from rebar.llm.config import LLMConfig
    from rebar.llm.prompting import prompts
    from rebar.llm.runner import RunRequest, get_runner

    cfg = LLMConfig.from_env()
    runner = get_runner(cfg)
    prompt = prompts.get_prompt("plan-review-verifier", repo_root=cfg.repo_path)

    def run_chunk(instructions: str, context: str) -> list[dict]:
        system, _meta = prompts.resolve_prompt(
            prompt,
            {"shared_prefix": prompts.shared_plan_prefix(context)},
            repo_root=cfg.repo_path,
        )
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
