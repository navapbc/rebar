"""Offline coverage for the epic-c81c fidelity spot-evals (stories c6e5 / 1762).

Proves the committed spot-eval harness (`plan_review.fidelity_spot_eval`):
  1. builds the right BASELINE (whole-in-system) vs CANDIDATE (volatile-split) request
     shapes for a relocated prompt,
  2. actually MEASURES fidelity — it PASSES when the two arms agree and FAILS when they
     diverge (so it is a real diff, not an asserted no-op), and
  3. re-checks the COMMITTED recorded live-run results against the same parity bar with no
     model call — making the measured evidence a regression-gated artifact.

No model/network: arm behavior is driven by a tiny injected runner.
"""

from __future__ import annotations

import pytest

from rebar.llm import prompts
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import fidelity_spot_eval as fse

pytestmark = pytest.mark.unit

_CTX = "CTXMARKER-unique-per-run-ticket-data"
_VARS = {"ticket_id": "T-1", "ticket_context": _CTX, "repo_path": "."}


def _fake_cfg() -> LLMConfig:
    return LLMConfig(runner="fake")


# ── 1. request-shape correctness (the relocation under test) ─────────────────────
def test_relocation_requests_split_volatile_out_of_system_prefix() -> None:
    cfg = _fake_cfg()
    base, cand = fse._relocation_requests(
        "completion-verifier", _VARS, base_instructions="verify", repo_root=None, cfg=cfg
    )
    # BASELINE keeps the per-run ticket data in the system prompt (pre-relocation shape).
    assert _CTX in base.system_prompt
    # CANDIDATE (shipped split) keeps it OUT of the cached prefix, in the user message.
    assert _CTX not in cand.system_prompt
    assert _CTX in cand.instructions
    # Same output contract on both arms — only placement differs.
    assert base.output_schema == cand.output_schema == "completion_verdict"
    assert base.mode == cand.mode == "structured"


def test_relocated_prompts_all_carry_the_split_marker() -> None:
    # The harness must cover exactly the prompts that were relocated (carry the marker).
    for pid in fse.RELOCATED_PROMPTS:
        assert prompts.VOLATILE_MARKER in prompts.get_prompt(pid).text, pid


# ── 2. the spot-eval MEASURES (passes on parity, fails on divergence) ────────────
class _ArmRunner:
    """A no-model runner whose verdict depends on whether the per-run data is in the
    system prompt (baseline) or not (candidate). ``diverge`` makes the candidate flip the
    verdict on the should-block fixtures — the regression the spot-eval must catch."""

    name = "arm"

    def __init__(self, *, diverge: bool):
        self._diverge = diverge

    def preflight(self):  # pragma: no cover - trivial
        pass

    def run(self, req):
        is_candidate = _CTX not in req.system_prompt
        wants_block = "SHOULD_BLOCK" in req.instructions or "SHOULD_BLOCK" in req.system_prompt
        verdict = "FAIL" if wants_block else "PASS"
        if self._diverge and is_candidate and wants_block:
            verdict = "PASS"  # candidate wrongly clears a should-block fixture
        return {"verdict": verdict, "findings": [], "summary": "s", "runner": "arm"}


def _corpus(n_block=6, n_clean=6):
    items = []
    for i in range(n_block):
        items.append(
            {
                "prompt_id": "completion-verifier",
                "variables": {**_VARS, "ticket_id": f"B{i}"},
                "base_instructions": "SHOULD_BLOCK",
                "label": "block",
            }
        )
    for i in range(n_clean):
        items.append(
            {
                "prompt_id": "completion-verifier",
                "variables": {**_VARS, "ticket_id": f"C{i}"},
                "base_instructions": "clean",
                "label": "advisory",
            }
        )
    return items


def test_spot_eval_passes_when_arms_agree() -> None:
    rep = fse.relocation_spot_eval(_corpus(), config=_fake_cfg(), runner=_ArmRunner(diverge=False))
    assert rep.passed, rep.gating_failures
    assert rep.metrics  # measured, not empty


def test_spot_eval_fails_when_candidate_diverges() -> None:
    # The candidate clears should-block fixtures → decision flips on gold → the spot-eval
    # MUST fail (proving it is a real fidelity measurement, not a trivial pass).
    rep = fse.relocation_spot_eval(_corpus(), config=_fake_cfg(), runner=_ArmRunner(diverge=True))
    assert not rep.passed
    assert any("flip" in f or "recall" in f for f in rep.gating_failures)


def test_spot_eval_respects_targeted_gold_floor() -> None:
    # Below SPOT_MIN_GOLD the bar fails on coverage (can't certify recall/false-accept).
    rep = fse.relocation_spot_eval(
        _corpus(n_block=1, n_clean=1), config=_fake_cfg(), runner=_ArmRunner(diverge=False)
    )
    assert not rep.passed
    assert any("gold set too small" in f for f in rep.gating_failures)


# ── 3. the committed recorded live-run evidence still clears the bar ─────────────
def test_recorded_live_results_are_within_tolerance() -> None:
    # Re-check the committed measured evidence (the last live run) offline: both the S2
    # relocation and S5 packing spot-evals must have PASSED. This regression-gates the
    # recorded artifact — if a future change degrades fidelity and the evidence is
    # refreshed with a failing run, this test fails.
    results = fse.load_recorded_results()
    assert set(results) >= {"s2_relocation", "s5_packing"}
    for name, rep in results.items():
        assert rep["passed"] is True, f"{name} recorded as FAILED: {rep.get('gating_failures')}"


def test_recorded_results_carry_measured_metrics() -> None:
    # The evidence is MEASURED (parity metrics present), not a bare assertion.
    results = fse.load_recorded_results()
    s2 = results["s2_relocation"]
    assert s2["metrics"], "no measured parity metrics recorded for the relocation spot-eval"
