"""Live Bedrock session experiment for ticket ed6d-5448-4ff6-4b8a.

Proves the ed6d acceptance criterion:

    "A real gate-style recovery over multiple criteria where at least one evidence
     run exhausts completes with a verdict instead of failing whole."

It drives the real ``CompletionAgentStep._recover`` (the exact code path ed6d
changes) over a synthetic three-criterion ticket. The FIRST criterion's evidence
run is forced to raise ``LLMBudgetExhaustedError``; the remaining two evidence runs
AND the tool-free finalizer execute LIVE against Amazon Bedrock. The step returns a
structured ``completion_verdict`` (``StepResult.status == "succeeded"``) instead of
raising ``CompletionRecoveryError`` — the whole-run abort the old code produced.

Run (requires live Bedrock credentials + region):

    AWS_REGION=us-east-1 \
    REBAR_LLM_STANDARD_PROVIDER=bedrock \
    REBAR_LLM_STANDARD_MODEL=us.anthropic.claude-sonnet-4-6 \
    python docs/experiments/ed6d_live_recovery.py

The transcript of a real run is recorded alongside this file in
``ed6d-live-recovery.md``. This is a session experiment, deliberately NOT part of
the unit suite (the unit oracle is
``tests/unit/workflow/test_completion_recovery_evidence_ed6d.py``).
"""

from __future__ import annotations

import dataclasses

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMBudgetExhaustedError
from rebar.llm.runner import RunRequest, get_runner
from rebar.llm.workflow import completion_recovery as cr
from rebar.llm.workflow import executor as ex

MODEL = "bedrock:us.anthropic.claude-sonnet-4-6"
CFG = dataclasses.replace(LLMConfig(), model=MODEL)
LIVE = get_runner(CFG)

SYNTHETIC_TICKET = {
    "ticket_id": "ed6d-live-experiment",
    "title": "Recovery partial-evidence live proof",
    "ticket_type": "task",
    "description": (
        "## Acceptance Criteria\n"
        "- [ ] The sky is blue on a clear day.\n"
        "- [ ] Water is wet.\n"
        "- [ ] Fire is hot.\n"
    ),
}


class _WrappingRunner:
    """Forces the FIRST evidence run to exhaust; every other call is live Bedrock."""

    def __init__(self) -> None:
        self.evidence_calls = 0
        self.finalizer_calls = 0
        self.live_evidence_calls = 0

    def run(self, req: RunRequest) -> dict:
        reviewers = " ".join(req.reviewers or [])
        if "evidence" in reviewers:
            self.evidence_calls += 1
            if self.evidence_calls == 1:
                exc = LLMBudgetExhaustedError(
                    "synthetic per-criterion budget exhaustion (finish_reason=length)"
                )
                exc.diagnostic = {"requests": 40, "tool_calls": 55}
                raise exc
            self.live_evidence_calls += 1
            return LIVE.run(dataclasses.replace(req, execution_mode="single_turn"))
        if "finalizer" in reviewers:
            self.finalizer_calls += 1
            return LIVE.run(req)
        raise AssertionError("unexpected non-evidence, non-finalizer runner call")


def main() -> None:
    import rebar._reads as reads

    orig = reads.show_ticket
    reads.show_ticket = lambda tid, repo_root=None: SYNTHETIC_TICKET  # type: ignore[assignment]
    try:
        wrapper = _WrappingRunner()
        step = cr.CompletionAgentStep(runner=wrapper, repo_root=None, config=CFG)
        ctx = ex.StepContext(
            run_id="ed6d-live",
            step_id="verify",
            kind="agent",
            step={},
            inputs={
                "ticket_id": "ed6d-live-experiment",
                "context": "Everyday physical facts about the world.",
            },
            workflow={},
            target_ticket=None,
            repo_root=None,
        )
        # The primary-exhausted precondition, injected straight into the unit under test.
        primary_exc = LLMBudgetExhaustedError("synthetic aggregate primary budget exhaustion")
        primary_exc.diagnostic = {"requests": 97, "tool_calls": 112}
        result = step._recover(ctx, primary_exc)
    finally:
        reads.show_ticket = orig

    verdict = result.outputs
    crits = verdict.get("criteria") or verdict.get("criterion_findings") or []
    print("=== ed6d LIVE recovery result ===")
    print("evidence runs total:", wrapper.evidence_calls,
          "(1 forced-exhausted, live:", wrapper.live_evidence_calls, ")")
    print("finalizer live calls:", wrapper.finalizer_calls)
    print("verdict keys:", sorted(verdict.keys()))
    print("verdict:", verdict.get("verdict"))
    print("verdict criteria count:", len(crits))
    print("STEP STATUS:", result.status)
    assert result.status == "succeeded", "recovery must complete with a verdict, not fail whole"


if __name__ == "__main__":
    main()
