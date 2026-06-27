"""Engine-wide prompt caching: the stable/volatile split (story c6e5 / S2).

S2 extends S1's anthropic prompt caching to the workflow agent steps by relocating
the per-run ticket/plan data OUT of the byte-stable system prefix (which the cache
reads) and INTO the user message. The split is driven by a `<!--volatile-->` marker in
the prompt body; the cache-aware RunRequest builders (RunnerAgentStep, code_review, the
review op) route everything after the marker to the user message via
`prompts.resolve_prompt_cached`, while non-splitting renderers strip the marker and keep
the whole prompt in the system slot. These offline tests pin the mechanism + the
byte-stability of the cached prefix (no live model).
"""

from __future__ import annotations

import pytest

from rebar.llm import prompts
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow.executor import StepContext
from rebar.llm.workflow.runs import RunnerAgentStep

pytestmark = pytest.mark.unit

# Every prompt that carries the cache-split marker (its prefix must be var-free).
_MARKED_PROMPTS = (
    "completion-verifier",
    "code-quality",
    "security",
    "tests",
    "ticket-quality",
    "plan-review-verifier",
    "plan-review-verifier-agentic",
    "plan-review-coach",
)


# ── split mechanism ─────────────────────────────────────────────────────────────
def test_split_volatile_splits_on_marker() -> None:
    stable, vol = prompts.split_volatile("stable text\n\n<!--volatile-->\nvolatile {{x}} body")
    assert stable == "stable text"
    assert vol == "volatile {{x}} body"


def test_split_volatile_no_marker_is_all_stable() -> None:
    # An unmarked prompt → the whole text is the (stable) system prompt, no volatile body
    # — exactly the pre-S2 behavior, so unmarked prompts are unchanged.
    assert prompts.split_volatile("all stable, no marker") == ("all stable, no marker", "")


def test_strip_volatile_marker_preserves_all_content() -> None:
    # The non-splitting renderers keep ALL content in the system prompt, just minus the
    # marker line — so adding a marker is fidelity-neutral for them (no reorder).
    out = prompts.strip_volatile_marker("role rules\n\n<!--volatile-->\n# Plan\n{{plan}}")
    assert "<!--volatile-->" not in out
    assert "role rules" in out and "# Plan" in out and "{{plan}}" in out


def test_resolve_prompt_cached_excludes_volatile_from_prefix() -> None:
    p = prompts.get_prompt("completion-verifier")
    stable, instructions, _meta = prompts.resolve_prompt_cached(
        p,
        {"ticket_id": "T-9", "ticket_context": "CTXMARKER-zzz", "repo_path": ""},
        base_instructions="DO THE TASK",
    )
    # The volatile per-run ticket context is NOT in the cached prefix...
    assert "CTXMARKER-zzz" not in stable
    # ...it is in the user-message instructions, ahead of the base task instruction.
    assert "CTXMARKER-zzz" in instructions and "DO THE TASK" in instructions
    assert instructions.index("CTXMARKER-zzz") < instructions.index("DO THE TASK")
    # The cached prefix is fully rendered (no leftover template vars to vary the bytes).
    assert "{{" not in stable


@pytest.mark.parametrize("pid", _MARKED_PROMPTS)
def test_marked_prompt_prefix_is_variable_free(pid: str) -> None:
    # The cached system prefix (everything before the marker) must reference NO {{vars}},
    # else the prefix bytes vary per run and the cache never reads.
    body = prompts.get_prompt(pid).text
    assert prompts.VOLATILE_MARKER in body, f"{pid} lost its cache-split marker"
    stable, _vol = prompts.split_volatile(body)
    assert "{{" not in stable, f"{pid} has a template var in its cached prefix"


def test_bespoke_plan_review_path_strips_marker_and_keeps_plan_in_system() -> None:
    # The plan-review batch/bespoke path (_resolve_system) must NEVER emit the literal
    # marker into the system prompt, and must keep the plan in system — content-identical
    # to pre-S2 for the marker-INSERTED (no-reorder) verifier/coach prompts, so adding the
    # marker is fidelity-neutral on that path (it only splits in RunnerAgentStep).
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import passes

    cfg = LLMConfig(runner="fake")
    sys_prompt = passes._resolve_system(passes.PASS_VERIFIER, "PLAN-XYZ-marker-test", cfg)
    assert prompts.VOLATILE_MARKER not in sys_prompt
    assert "PLAN-XYZ-marker-test" in sys_prompt  # plan stays in system on the bespoke path


def test_prompt_file_impact_front_matter_parsed() -> None:
    # file_impact is a first-class front-matter key on the prompt model (S2).
    assert prompts.get_prompt("completion-verifier").file_impact == [
        "src/rebar/llm/workflow/runs.py",
        "src/rebar/llm/prompts.py",
    ]
    assert prompts.get_prompt("code-quality").file_impact == []  # default empty


# ── RunnerAgentStep: byte-stable cached prefix across runs ───────────────────────
class _CapturingRunner(FakeRunner):
    """A FakeRunner that records every RunRequest it is handed (offline)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.reqs: list = []

    def run(self, req):
        self.reqs.append(req)
        return super().run(req)


def _ctx(step: dict, inputs: dict) -> StepContext:
    return StepContext(
        run_id="r",
        step_id=step["id"],
        kind="agent",
        step=step,
        inputs=inputs,
        workflow={"name": "wf"},
        target_ticket=inputs.get("ticket_id", "T"),
        repo_root=None,
    )


def test_verify_step_prefix_is_byte_stable_across_plans() -> None:
    runner = _CapturingRunner(structured={"verifications": []})
    step = {
        "id": "verify",
        "prompt": "plan-review-verifier",
        "mode": "structured",
        "output_schema": "plan_review_verification",
    }
    rs = RunnerAgentStep(runner=runner, repo_root=None)
    rs.run(_ctx(step, {"ticket_id": "T-1", "plan": "PLAN-ALPHA-aaa", "findings": []}))
    rs.run(_ctx(step, {"ticket_id": "T-2", "plan": "PLAN-BETA-bbb", "findings": []}))
    r1, r2 = runner.reqs
    # The cached system prefix is byte-identical across two different plans...
    assert r1.system_prompt == r2.system_prompt
    # ...the per-run plan is NOT in the cached prefix...
    assert "PLAN-ALPHA-aaa" not in r1.system_prompt
    assert "PLAN-BETA-bbb" not in r2.system_prompt
    # ...it is relocated into the user-message instructions.
    assert "PLAN-ALPHA-aaa" in r1.instructions
    assert "PLAN-BETA-bbb" in r2.instructions


def test_completion_verifier_prefix_is_byte_stable_across_tickets() -> None:
    runner = _CapturingRunner(structured={"verdict": "PASS", "findings": [], "summary": "s"})
    step = {"id": "verify", "prompt": "completion-verifier"}
    rs = RunnerAgentStep(runner=runner, repo_root=None)
    rs.run(_ctx(step, {"ticket_id": "T-1", "ticket_context": "TICKET-CTX-aaa"}))
    rs.run(_ctx(step, {"ticket_id": "T-2", "ticket_context": "TICKET-CTX-bbb"}))
    r1, r2 = runner.reqs
    assert r1.system_prompt == r2.system_prompt  # close-gate prompt caches a stable prefix
    assert "TICKET-CTX-aaa" not in r1.system_prompt
    assert "TICKET-CTX-bbb" not in r2.system_prompt
    assert "TICKET-CTX-aaa" in r1.instructions
    assert "TICKET-CTX-bbb" in r2.instructions
