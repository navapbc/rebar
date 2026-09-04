from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.unit


def test_old_private_shim_modules_no_longer_export_moved_bindings() -> None:
    old_bindings = {
        "rebar.llm.runner": {
            "_DIRECT_ANTHROPIC_BASE_URL",
            "_local_proxy_bypass_base_url",
            "_TOOL_CAPABILITY_CHECKED",
            "effective_max_iterations",
            "effective_max_tokens",
        },
        "rebar.llm.config": {
            "current_code_root",
            "resolve_code_root",
            "current_tickets_root",
            "current_code_sha",
            "in_gate_session",
            "gate_session",
            "assert_gated",
            "use_code_root",
            "use_tickets_root",
            "_active_code_root",
            "_active_tickets_root",
            "_in_gate_session",
        },
        "rebar.llm.workflow.gate_dispatch": {
            "STEP_PRECHECK",
            "STEP_ASSEMBLE",
            "STEP_FINDERS",
            "STEP_VERIFY",
            "STEP_DECIDE",
            "STEP_COACH",
            "GateContractError",
            "_collect_step_ids",
            "_validate_gate_step_ids",
            "_attach_plan_review_metrics",
            "_recover_plan_review_coach_failure",
            "_recover_plan_review_verify_failure",
            "_degraded_plan_review_verdict",
        },
        "rebar.llm.plan_review.orchestrator": {
            "largest_window_tokens",
            "_centrality",
            "_models_at_or_above",
            "_shed_to_budget",
            "MOVE_REGISTRY",
            "load_move_registry",
            "assemble_context",
            "assemble_context_cache",
            "material_fingerprint",
        },
        "rebar.llm.plan_review.sizing": {
            "checkpoint_identity",
            "load_checkpoint",
            "save_checkpoint",
            "plan_budget_cap",
            "shed_to_budget",
            "model_max_output_tokens",
            "max_output_cfg",
        },
        "rebar.llm.plan_review.passes": {
            "pass3_decide",
            "validity",
            "impact",
            "GRADED_BINARY",
            "verify_instructions",
            "MOVE_REGISTRY",
            "load_move_registry",
            "triage_advisories",
            "pass2_completion",
            "completion_floor_drop",
        },
    }
    leaks = {
        module_name: sorted(
            name for name in names if hasattr(importlib.import_module(module_name), name)
        )
        for module_name, names in old_bindings.items()
    }
    assert {module: names for module, names in leaks.items() if names} == {}


def test_canonical_owners_still_provide_the_migrated_behaviour() -> None:
    from rebar.llm import anthropic_model, gate_context, runner_support, structured_run
    from rebar.llm.plan_review import budget, checkpoints, coach_moves, context_assembly
    from rebar.llm.review_kernel import decide as kernel_decide
    from rebar.llm.review_kernel import verify as kernel_verify
    from rebar.llm.workflow import plan_review_recovery

    kernel_coach = importlib.import_module("rebar.llm.review_kernel.coach")
    assert anthropic_model._DIRECT_ANTHROPIC_BASE_URL == "https://api.anthropic.com"
    assert callable(runner_support._check_tool_capability)
    assert callable(structured_run.effective_max_iterations)
    assert gate_context.current_code_root() is None
    assert callable(context_assembly.assemble_context_cache)
    assert callable(coach_moves.load_move_registry)
    assert callable(checkpoints.checkpoint_identity)
    assert callable(budget.plan_budget_cap)
    assert kernel_decide.pass3_decide({"binary": {q: "yes" for q in kernel_decide.GRADED_BINARY}})
    assert callable(kernel_verify.verify_instructions)
    assert callable(kernel_coach.render_coach_notes)
    assert plan_review_recovery.STEP_PRECHECK == "precheck"
