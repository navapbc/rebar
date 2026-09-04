"""Plan-first shared cache prefix across passes + the Pass-1 exhaustiveness directive
(story 9374-bffb-9483-42f7).

The Pass-1 finder system prompt and the rendered stable segments of BOTH Pass-2
verifier prompts must begin, byte-for-byte, with ``prompts.shared_plan_prefix(plan)``
— the single-sourced reviewing-stance preamble (now carrying the exhaustiveness
directive) followed by the full plan material. The per-run finding listing stays
user-turn (``base_instructions``); the Pass-1 warm gate measures the ACTUAL prefix
bytes. All offline — FakeRunner / no model.
"""

from __future__ import annotations

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import pass1, passes
from rebar.llm.plan_review.det_floor import est_tokens
from rebar.llm.prompting import prompts
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow.executor import StepContext
from rebar.llm.workflow.runs import RunnerAgentStep

pytestmark = pytest.mark.unit

# A named fixture plan sized so the SHARED PREFIX comfortably exceeds the cache floor
# (est_tokens is chars/4, deterministic — the test_pass1_warmup.py sizing approach).
FIXTURE_PLAN = "## Plan\nBuild the widget in src/widget.py.\n" + (
    "The implementation proceeds in small verified steps. " * 400
)

_VERIFIER_PROMPTS = ("plan-review-verifier", "plan-review-verifier-agentic")
_LISTING = "### finding index 0\nclaim: SENTINEL-FINDING-LISTING-xyz"


def _cfg() -> LLMConfig:
    return LLMConfig(runner="fake")


# ── byte-identical plan-bearing leading prefix ──────────────────────────────────
def test_pass1_finder_system_starts_with_shared_prefix() -> None:
    system = passes._resolve_system(passes.PASS_FINDER, FIXTURE_PLAN, _cfg())
    prefix = prompts.shared_plan_prefix(FIXTURE_PLAN)
    assert system.startswith(prefix)
    assert FIXTURE_PLAN in prefix  # the prefix carries the full plan material


@pytest.mark.parametrize("pid", _VERIFIER_PROMPTS)
def test_verifier_stable_segment_starts_with_shared_prefix(pid: str) -> None:
    prefix = prompts.shared_plan_prefix(FIXTURE_PLAN)
    stable, instructions, _meta = prompts.resolve_prompt_cached(
        prompts.get_prompt(pid),
        {"shared_prefix": prefix},
        base_instructions=_LISTING,
    )
    assert stable.startswith(prefix)  # byte-identical leading prefix, by construction
    assert prompts.VOLATILE_MARKER not in stable  # no marker remains in the template
    # The per-run finding listing stays USER-TURN, never in the stable system segment.
    assert _LISTING not in stable
    assert _LISTING in instructions


# ── single-sourced preamble ─────────────────────────────────────────────────────
def test_shared_preamble_is_single_sourced() -> None:
    assert not hasattr(passes, "_SHARED_PREAMBLE")
    # A non-restructured pass still leads with the stance preamble — exactly once.
    system = passes._resolve_system(passes.PASS_ISF, "PLAN-BODY-MARKER", _cfg())
    assert system.startswith(prompts.SHARED_STANCE_PREAMBLE)
    assert system.count(prompts.SHARED_STANCE_PREAMBLE) == 1


def test_finder_prefix_contains_preamble_exactly_once() -> None:
    system = passes._resolve_system(passes.PASS_FINDER, "PLAN-BODY-MARKER", _cfg())
    assert system.count(prompts.SHARED_STANCE_PREAMBLE) == 1


# ── the exhaustiveness directive reaches every Pass-1 request ───────────────────
_DIRECTIVE = "enumerate EVERY independent defect"


def test_directive_is_in_the_preamble_and_prefix() -> None:
    assert _DIRECTIVE in prompts.SHARED_STANCE_PREAMBLE
    assert _DIRECTIVE in prompts.shared_plan_prefix("p")


@pytest.mark.parametrize("pid", [passes.PASS_FINDER, passes.PASS_CONTAINER, passes.PASS_ISF])
def test_directive_reaches_every_pass1_entry_point(pid: str) -> None:
    assert _DIRECTIVE in passes._resolve_system(pid, "PLAN-BODY-MARKER", _cfg())


# ── cache floor: the prefix meets the codebase constant ─────────────────────────
def test_fixture_prefix_meets_the_cache_floor() -> None:
    assert est_tokens(prompts.shared_plan_prefix(FIXTURE_PLAN)) >= pass1.CACHE_MIN_PREFIX_TOKENS


def test_warm_gate_measures_the_actual_prefix() -> None:
    # Boundary plan: alone it is BELOW the floor, but the ACTUAL cached bytes
    # (shared_plan_prefix) are at/above it — the gate must measure the latter.
    boundary_plan = "p" * (pass1.CACHE_MIN_PREFIX_TOKENS * 4 - 40)
    assert est_tokens(boundary_plan) < pass1.CACHE_MIN_PREFIX_TOKENS
    assert pass1._prefix_tokens(boundary_plan) >= pass1.CACHE_MIN_PREFIX_TOKENS


# ── the live render path resolves with yaml-shaped inputs ───────────────────────
@pytest.mark.parametrize("pid", _VERIFIER_PROMPTS)
def test_runner_agent_step_renders_verifier_with_yaml_shaped_inputs(pid: str) -> None:
    step = {
        "id": "verify",
        "prompt": pid,
        "mode": "structured",
        "output_schema": "plan_review_verification",
    }
    ctx = StepContext(
        run_id="r",
        step_id="verify",
        kind="agent",
        step=step,
        inputs={
            "ticket_id": "T-1",
            "shared_prefix": prompts.shared_plan_prefix(FIXTURE_PLAN),
            "findings": [],
            "instructions": _LISTING,
        },
        workflow={"name": "plan-review"},
        target_ticket="T-1",
        repo_root=None,
    )
    runner = FakeRunner(structured={"verifications": []})
    res = RunnerAgentStep(runner=runner, repo_root=None).run(ctx)
    assert res.status == "succeeded"


def test_verify_inputs_step_emits_shared_prefix_not_plan(monkeypatch) -> None:
    # The scripted seam feeding the yaml `with:` blocks — its output keys are the contract.
    from types import SimpleNamespace

    import rebar.llm.plan_review as pr
    from rebar.llm.plan_review import context_assembly, workflow_ops

    monkeypatch.setattr(
        context_assembly,
        "assemble_context",
        lambda tid, repo_root=None: SimpleNamespace(plan_text="PLAN-TEXT"),
    )
    monkeypatch.setattr(
        "rebar.llm.config.resolve_gate_config", lambda repo_root: LLMConfig(runner="fake")
    )
    monkeypatch.setattr(pr, "_verifier_cfg", lambda cfg: cfg)
    ctx = StepContext(
        run_id="r",
        step_id="verify_inputs",
        kind="uses",
        step={"id": "verify_inputs", "uses": "plan_review_verify_inputs"},
        inputs={"ticket_id": "T-1", "findings": []},
        workflow={"name": "plan-review"},
        target_ticket="T-1",
        repo_root=None,
    )
    out = workflow_ops.plan_review_verify_inputs(ctx)
    assert set(out) == {"shared_prefix", "instructions"}
    assert out["shared_prefix"] == prompts.shared_plan_prefix("PLAN-TEXT")


def test_yaml_and_schema_wire_shared_prefix_not_plan() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    yaml_text = (root / "src/rebar/llm/workflow/gates/plan-review.yaml").read_text()
    assert yaml_text.count("${{ steps.verify_inputs.outputs.shared_prefix }}") == 2
    assert "steps.verify_inputs.outputs.plan" not in yaml_text
    schema_raw = (
        root / "src/rebar/schemas/plan_review_verify_inputs_output.schema.json"
    ).read_text()
    schema = json.loads(schema_raw)
    assert "shared_prefix" in schema["properties"]
    assert "shared_prefix" in schema["required"]
    assert "plan" not in schema["properties"]
    assert "{{plan}}" not in schema_raw


# ── bug 1dbe: prerequisite verifier reordered to LEAD with the shared prefix ──────
def test_prerequisite_verifier_leads_with_shared_prefix_no_content_lost() -> None:
    """The Pass-2 prerequisite verifier's resolved system prompt must now begin,
    byte-for-byte, with ``shared_plan_prefix(plan)`` — so its cache breakpoint lands at
    the same boundary the finder writes (bug 1dbe) — WITHOUT dropping any stance content
    or the whole plan material (the model-visible reorder is content-preserving)."""
    prompt = prompts.get_prompt(passes.PASS_PREREQUISITE_VERIFIER)
    prefix = prompts.shared_plan_prefix(FIXTURE_PLAN)
    system, _meta = prompts.resolve_prompt(prompt, {"shared_prefix": prefix})
    # Cache-prefix alignment: byte-identical leading prefix (same seam as the finder).
    assert system.startswith(prefix)
    assert FIXTURE_PLAN in prefix  # the prefix still carries the full plan material
    # No content dropped by the reorder: the pass-specific stance survives, after the plan.
    assert "Independently verify each listed prerequisite-consistency finding" in system
    assert "prerequisite_attribution_valid" in system
    # The template no longer carries a separate trailing ``{{plan}}`` block.
    assert "{{plan}}" not in prompt.text


def test_prerequisite_verify_inputs_emits_shared_prefix() -> None:
    from rebar.llm.plan_review import prerequisite_workflow_ops as ops

    ctx = StepContext(
        run_id="r",
        step_id="prerequisite_verify_inputs",
        kind="uses",
        step={"id": "prerequisite_verify_inputs", "uses": "plan_review_prerequisite_verify_inputs"},
        inputs={"subject_plan": FIXTURE_PLAN, "findings": [], "prerequisites": []},
        workflow={"name": "plan-review"},
        target_ticket="T-1",
        repo_root=None,
    )
    out = ops.plan_review_prerequisite_verify_inputs(ctx)
    assert out["shared_prefix"] == prompts.shared_plan_prefix(FIXTURE_PLAN)
