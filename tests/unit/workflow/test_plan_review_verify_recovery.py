"""Pass-2 verify-failure robustness for the plan-review gate (bug 59bc).

When the agentic Pass-2 verifier exhausts its step budget on a finding-rich ticket, the
old gate misclassified the step failure as ``llm_unavailable`` → a hollow INDETERMINATE
that DISCARDED the Pass-1 findings and (fail-closed) wrongly blocked the claim. The fix:

* the agentic verify step budget SCALES with the finding count (``step_budget_per_item``);
* a verify-step failure is RECOVERED — Pass-1 findings are preserved (unverified →
  INDETERMINATE) and the verdict fails OPEN unless a preserved finding sits on a
  blocking-enabled criterion (then INDETERMINATE, fail-closed).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebar.llm.config import DEFAULT_MAX_TOKENS, LLMConfig
from rebar.llm.plan_review import orchestrator
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner, effective_max_iterations, effective_max_tokens
from rebar.llm.workflow import gate_dispatch
from rebar.llm.workflow.executor import StepContext
from rebar.llm.workflow.runs import RunnerAgentStep

pytestmark = pytest.mark.unit


# ── budget scaling: the agentic verifier's step budget scales with the finding count ──────────
class _CapturingRunner(FakeRunner):
    """Records the per-request budgets (max_iterations / max_tokens) and call count."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.max_iterations: int | None = None
        self.max_tokens: int | None = None
        self.calls: int = 0

    def run(self, req):
        self.calls += 1
        self.max_iterations = req.config.max_iterations
        self.max_tokens = getattr(req.config, "max_tokens", None)
        return super().run(req)


def _verify_ctx(inputs: dict) -> StepContext:
    return StepContext(
        run_id="r",
        step_id="verify",
        kind="agent",
        step={
            "id": "verify",
            "prompt": "plan-review-verifier-agentic",
            "mode": "structured",
            "output_schema": "plan_review_verification",
        },
        inputs={"ticket_id": "T-1", "plan": "## Plan\nBuild X in src/x.py.", **inputs},
        workflow={"name": "plan-review"},
        target_ticket="T-1",
        repo_root=None,
    )


def test_verify_budget_scales_with_finding_count() -> None:
    findings = [{"finding": f"f{i}", "criteria": ["G6"]} for i in range(30)]
    runner = _CapturingRunner(structured={"verifications": []})
    ctx = _verify_ctx({"findings": findings, "instructions": "verify", "step_budget_per_item": 25})
    res = RunnerAgentStep(runner=runner, repo_root=None).run(ctx)
    assert res.status == "succeeded"
    assert runner.max_iterations >= 25 * len(findings)  # scaled (25×30 = 750), not the default 50


def test_verify_budget_unscaled_without_the_input() -> None:
    """Absent ``step_budget_per_item``, the step keeps the configured default (no scaling)."""
    default = LLMConfig.from_env().max_iterations
    findings = [{"finding": f"f{i}", "criteria": ["G6"]} for i in range(30)]
    runner = _CapturingRunner(structured={"verifications": []})
    ctx = _verify_ctx({"findings": findings, "instructions": "verify"})  # no per-item budget
    RunnerAgentStep(runner=runner, repo_root=None).run(ctx)
    assert runner.max_iterations == default


def test_runner_honors_per_request_budget_not_just_self_config() -> None:
    """The runner's effective step budget is the PER-REQUEST max (``req.config``), raised above
    the operator floor. This is the seam the scaled verifier budget flows through — the old
    runner read only ``self._config`` and ignored a scaled ``req.config`` (the false-green this
    closes: RunnerAgentStep set req.config, but the live runner never read it → bug 59bc)."""
    assert effective_max_iterations(50, 475) == 475  # a request RAISES the floor
    assert effective_max_iterations(50, None) == 50  # no override → the floor
    assert effective_max_iterations(100, 50) == 100  # a request can NEVER lower the floor


# ── output-cap scaling: the verifier's per-CALL max_tokens scales with finding count ──────────
# Bug spy-luge-wool (=b54e) / sole-teal-churn: the Pass-2 verify structured output grows ~1
# verification per finding, so a FIXED per-call max_tokens truncates (finish_reason=length) on a
# finding-rich plan and the whole review collapses to INDETERMINATE. The turn budget already
# scales (above); the OUTPUT cap must scale the same way.
def test_verify_output_cap_scales_with_finding_count() -> None:
    findings = [{"finding": f"f{i}", "criteria": ["G6"]} for i in range(30)]
    runner = _CapturingRunner(structured={"verifications": []})
    ctx = _verify_ctx(
        {
            "findings": findings,
            "instructions": "verify",
            "step_budget_per_item": 25,
            "output_tokens_per_item": 2000,
        }
    )
    res = RunnerAgentStep(runner=runner, repo_root=None).run(ctx)
    assert res.status == "succeeded"
    # 2000 × 30 = 60000, well above the 16000 default — enough to emit 30 verifications.
    assert runner.max_tokens is not None and runner.max_tokens >= 2000 * len(findings)
    assert runner.max_tokens > DEFAULT_MAX_TOKENS


def test_verify_output_cap_unscaled_without_the_input() -> None:
    """Absent ``output_tokens_per_item`` the step keeps the configured cap (a tiny ticket is
    unaffected — the floor is never lowered)."""
    default = LLMConfig.from_env().max_tokens
    findings = [{"finding": f"f{i}", "criteria": ["G6"]} for i in range(30)]
    runner = _CapturingRunner(structured={"verifications": []})
    ctx = _verify_ctx({"findings": findings, "instructions": "verify"})  # no per-item token knob
    RunnerAgentStep(runner=runner, repo_root=None).run(ctx)
    assert runner.max_tokens == default


def test_runner_honors_per_request_output_cap() -> None:
    """The effective per-call output cap is max(operator floor, per-request), mirroring the
    turn-budget seam — so a scaled verifier cap on ``req.config`` is actually applied and can
    never lower the configured floor."""
    assert effective_max_tokens(16000, 60000) == 60000  # a request RAISES the floor
    assert effective_max_tokens(16000, None) == 16000  # no override → the floor
    assert effective_max_tokens(32000, 16000) == 32000  # a request can NEVER lower the floor


# ── turf-purple-dot: an empty instructions list must never silently zero-dispatch verify ──────
def test_empty_instructions_still_dispatches_and_warns(caplog) -> None:
    """A literal empty ``instructions`` list made the chunk loop run the model ZERO times and
    merge to ``{}`` — a SILENT no-op (verify_requests=0, empty stderr) that degraded every
    finding to no-verification. The step must instead dispatch one aggregate call AND log a
    diagnostic, never a hollow success."""
    import logging

    findings = [{"finding": f"f{i}", "criteria": ["G6"]} for i in range(3)]
    runner = _CapturingRunner(structured={"verifications": []})
    ctx = _verify_ctx({"findings": findings, "instructions": []})  # the degenerate empty list
    with caplog.at_level(logging.WARNING):
        res = RunnerAgentStep(runner=runner, repo_root=None).run(ctx)
    assert runner.calls >= 1, "verify must dispatch at least one call, not zero (silent no-op)"
    assert res.status == "succeeded"
    assert any("instruction" in r.getMessage().lower() for r in caplog.records), (
        "a degraded/empty verify dispatch must emit a diagnostic, never be silent"
    )


# ── the verdict rule: a verify failure fails OPEN unless a finding is potentially-blocking ────
def _parts(indeterminate: list[dict]) -> dict[str, list]:
    return {
        "blocking": [],
        "surfaced": [],
        "overflow": [],
        "indeterminate": indeterminate,
        "dropped": [],
    }


def _ctx() -> PlanContext:
    return PlanContext(ticket_id="T-1", ticket_type="task", title="t", description="d")


def test_verify_failed_fails_open_when_no_blocking_criterion() -> None:
    """All preserved findings on advisory-only criteria → can never block → PASS (fail-open)."""
    v = orchestrator.finalize_verdict(
        _ctx(),
        _parts([{"id": "f1", "criteria": ["A1"], "decision": "indeterminate"}]),  # A1 = advisory
        coaching=[],
        coverage={"verify_failed": True, "llm_ran": True},
        runner_name="fake",
        model=None,
    )
    assert v["verdict"] == "PASS"
    assert v["indeterminate"], "the Pass-1 finding is PRESERVED, not discarded"


def test_verify_failed_fails_closed_when_a_blocking_criterion_present() -> None:
    """A preserved finding on a blocking-enabled criterion (G6) is potentially-blocking — with
    no Pass-2 we cannot rule out a block → INDETERMINATE (fail-closed)."""
    v = orchestrator.finalize_verdict(
        _ctx(),
        _parts([{"id": "f1", "criteria": ["G6"], "decision": "indeterminate"}]),  # G6 = blocking
        coaching=[],
        coverage={"verify_failed": True, "llm_ran": True},
        runner_name="fake",
        model=None,
    )
    assert v["verdict"] == "INDETERMINATE"


def test_normal_all_indeterminate_unchanged() -> None:
    """Regression: WITHOUT verify_failed, the existing all-indeterminate rule still applies."""
    v = orchestrator.finalize_verdict(
        _ctx(),
        _parts([{"id": "f1", "criteria": ["A1"], "decision": "indeterminate"}]),
        coaching=[],
        coverage={"llm_ran": True},  # no verify_failed
        runner_name="fake",
        model=None,
    )
    assert v["verdict"] == "INDETERMINATE"


# ── the recovery seam: a verify failure preserves findings + sets verify_failed (not unavailable) ─
def _rec(steps: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(steps=steps)


def _succeeded(step_id: str, outputs: dict) -> dict:
    return {"status": "succeeded", "frame_key": f"review/then/{step_id}", "outputs": outputs}


def test_recover_verify_failure_preserves_findings_and_does_not_block_falsely() -> None:
    """finders succeeded + decide absent (verify failed) → findings preserved, verify_failed set,
    NOT llm_unavailable; an advisory-only finding fails OPEN to PASS."""
    rec = _rec(
        [
            {
                "status": "succeeded",
                "frame_key": "precheck",
                "outputs": {
                    "det_coverage": {},
                    "det_blocking": [],
                    "det_advisory": [],
                    "canonical_id": "T-1",
                    "ticket_type": "task",
                },
            },
            _succeeded("assemble", {"routing": {"single_turn": ["A1"]}}),
            _succeeded("finders", {"findings": [{"finding": "uses A1", "criteria": ["A1"]}]}),
        ]
    )
    verdict = gate_dispatch._recover_plan_review_verify_failure(
        rec, LLMConfig(runner="fake"), error="agent exceeded its step budget"
    )
    assert verdict is not None
    assert verdict["coverage"]["verify_failed"] is True
    assert "llm_unavailable" not in verdict["coverage"]
    assert verdict["indeterminate"], "Pass-1 findings preserved as INDETERMINATE"
    assert verdict["verdict"] == "PASS"  # A1 is advisory → fail-open


def test_recover_returns_none_when_finders_did_not_run() -> None:
    """No finders output → a genuine LLM-tier failure (not verify-only) → caller degrades."""
    rec = _rec([{"status": "succeeded", "frame_key": "precheck", "outputs": {"canonical_id": "T"}}])
    assert (
        gate_dispatch._recover_plan_review_verify_failure(rec, LLMConfig(runner="fake"), error="x")
        is None
    )


# ── step-id contract: a YAML rename is caught LOUDLY, not silently degraded ────────────────────
# The recovery/metrics reconstruction above looks up succeeded-step partitions BY STEP ID
# (STEP_PRECHECK/STEP_ASSEMBLE/STEP_FINDERS/STEP_VERIFY/STEP_DECIDE). If gates/plan-review.yaml
# renamed one of those steps, the lookup would silently return None and a recoverable run would
# degrade to a hollow INDETERMINATE that DISCARDS real findings — with no error. The dispatcher
# now validates the referenced ids against the loaded doc at dispatch time; these prove it.
def _rename_all_steps(node, old: str, new: str) -> None:
    """Recursively rename every step whose ``id`` == ``old`` to ``new`` (steps appear in nested
    ``branch`` arms, so a top-level pass is not enough)."""
    if isinstance(node, dict):
        if node.get("id") == old:
            node["id"] = new
        for value in node.values():
            _rename_all_steps(value, old, new)
    elif isinstance(node, list):
        for item in node:
            _rename_all_steps(item, old, new)


def test_collect_step_ids_includes_nested_branch_steps() -> None:
    """The id collector reaches steps nested inside ``branch`` then/else arms (verify/decide/coach
    live inside the review branch), so the contract check sees the WHOLE workflow, not just the
    top-level steps."""
    doc = gate_dispatch._gate_doc("plan-review", None)
    ids = gate_dispatch._collect_step_ids(doc.get("steps"))
    for nested in (gate_dispatch.STEP_VERIFY, gate_dispatch.STEP_DECIDE, gate_dispatch.STEP_COACH):
        assert nested in ids, f"{nested} (a branch-nested step) must be collected"
    assert gate_dispatch._PLAN_REVIEW_REQUIRED_STEP_IDS <= ids


def test_validate_gate_step_ids_passes_on_real_doc() -> None:
    """The packaged gate doc satisfies the contract (no raise) — a guard against a false alarm."""
    doc = gate_dispatch._gate_doc("plan-review", None)
    gate_dispatch._validate_gate_step_ids(
        doc, gate_dispatch._PLAN_REVIEW_REQUIRED_STEP_IDS, gate_name="plan-review"
    )


def test_validate_gate_step_ids_raises_on_renamed_step() -> None:
    """A required step renamed out from under the recovery code raises GateContractError (LOUD),
    and the message names the missing id — not a silent None/degrade."""
    import copy

    drifted = copy.deepcopy(gate_dispatch._gate_doc("plan-review", None))
    _rename_all_steps(drifted, gate_dispatch.STEP_DECIDE, "decide_RENAMED")
    with pytest.raises(gate_dispatch.GateContractError) as exc:
        gate_dispatch._validate_gate_step_ids(
            drifted, gate_dispatch._PLAN_REVIEW_REQUIRED_STEP_IDS, gate_name="plan-review"
        )
    assert gate_dispatch.STEP_DECIDE in str(exc.value)


def test_produce_plan_review_verdict_raises_loudly_on_step_rename(monkeypatch) -> None:
    """END-TO-END: a step rename in the loaded doc makes dispatch raise GateContractError BEFORE
    the billable run — it does NOT fall through to a degraded INDETERMINATE. This is the exact
    silent-degrade the validation prevents."""
    import copy

    drifted = copy.deepcopy(gate_dispatch._gate_doc("plan-review", None))
    _rename_all_steps(drifted, gate_dispatch.STEP_PRECHECK, "precheck_RENAMED")
    monkeypatch.setattr(gate_dispatch, "_gate_doc", lambda name, repo_root: drifted)

    class _StubRunner:
        name = "fake"

        def preflight(self) -> None:  # passes preflight so we reach the doc validation
            return None

    ctx = SimpleNamespace(ticket_id="T-1")
    with pytest.raises(gate_dispatch.GateContractError):
        gate_dispatch.produce_plan_review_verdict(
            ctx, LLMConfig(runner="fake"), runner=_StubRunner(), advisory_cap=5, repo_root=None
        )
