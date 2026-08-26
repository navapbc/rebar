"""Code review's Pass-2 verifier resolves the `standard` class (task 172e, the remaining gap).

Change 1119 moved plan-review's `_verifier_cfg` and the completion verifier onto the standard
class; `tests/unit/test_verifier_class.py` covers those. CODE REVIEW was left behind, and it was
the worst-affected path: `workflow/gates/code-review.yaml`'s `verify` step declared no `model:` at
all, so it fell through to `cfg.model` — the FRONTIER model. Two measured consequences:

* no cost downgrade on Pass-2, the pass that runs once per merged finding;
* the step's `temperature: 0` NEVER took effect. pydantic-ai's Anthropic adapter silently drops
  temperature for the frontier model, so the greedy determinism the verification contract depends
  on was never actually in force.

Adding `model: standard` only became possible once sibling 7761 taught `config.resolve_model` to
resolve reserved class names; before that it would have passed the LITERAL string `standard` to the
runner as if it were a model id. The workflow engine reaches it at `workflow/runs.py`:
`resolve_model(cfg, step=ctx.step.get("model"), workflow=ctx.workflow.get("model"))`.

THE OBSERVABLE here is the model id the engine would resolve for that step — the YAML declaration
run through the real `resolve_model` — not a hand-rolled reimplementation of the lookup.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml
from _tree_scan import parsed_python_files

from rebar.llm.config import LLMConfig, infer_provider, resolve_model

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATES = _REPO_ROOT / "src" / "rebar" / "llm" / "workflow" / "gates"
_CODE_REVIEW_YAML = _GATES / "code-review.yaml"

_BEDROCK_STANDARD = "bedrock:us.anthropic.claude-sonnet-4-6"
_BEDROCK_FRONTIER = "bedrock:us.anthropic.claude-opus-4-8"

_ENV_VARS = tuple(
    f"REBAR_LLM_{cls}_{field}"
    for cls in ("TRIVIAL", "STANDARD", "FRONTIER")
    for field in ("MODEL", "PROVIDER", "ENDPOINT")
)


def _set_class_table(monkeypatch, table: dict[str, Any] | None) -> None:
    """Point `load_class_slots` at `table` and clear the nine env overrides, so an ambient
    override on the developer's machine cannot retarget a class under the test."""
    from rebar.llm import config as llm_config

    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        llm_config, "_read_llm_file_table", lambda repo_root=None: dict(table or {})
    )


@pytest.fixture
def bedrock_classes(monkeypatch):
    """Every class retargeted onto Bedrock — the S8 cutover configuration."""
    _set_class_table(
        monkeypatch,
        {
            "model_classes": {
                "trivial": {"model": "us.anthropic.claude-haiku-4-5", "provider": "bedrock"},
                "standard": {"model": "us.anthropic.claude-sonnet-4-6", "provider": "bedrock"},
                "frontier": {"model": "us.anthropic.claude-opus-4-8", "provider": "bedrock"},
            }
        },
    )


def _step(gate_yaml: Path, step_id: str) -> dict[str, Any]:
    """The raw step mapping for `step_id`, read from the gate definition itself so the test
    cannot drift from the shipped YAML."""
    doc = yaml.safe_load(gate_yaml.read_text())
    for step in doc.get("steps", []) or []:
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"no step {step_id!r} in {gate_yaml.name}")


def _engine_resolved_model(gate_yaml: Path, step_id: str, cfg: LLMConfig) -> str:
    """What `workflow/runs.py` would resolve for this step: the real precedence function applied
    to the step's declared `model:` and the workflow-level default."""
    doc = yaml.safe_load(gate_yaml.read_text())
    step = _step(gate_yaml, step_id)
    return resolve_model(cfg, step=step.get("model"), workflow=doc.get("model"))


# ══ HAPPY PATH ════════════════════════════════════════════════════════════════════════


def test_code_review_verify_step_declares_the_standard_class() -> None:
    """AC: code review's verify step names the `standard` class rather than falling through to
    `cfg.model`. Declared in the gate YAML, which is the operator-visible contract."""
    assert _step(_CODE_REVIEW_YAML, "verify").get("model") == "standard"


def test_code_review_verify_step_resolves_to_the_configured_standard_model(
    bedrock_classes,
) -> None:
    """AC: that declaration resolves THROUGH the class config, so the operator retargets Pass-2 by
    configuring the standard class — not by editing the gate definition."""
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-opus-4-8")
    assert _engine_resolved_model(_CODE_REVIEW_YAML, "verify", cfg) == _BEDROCK_STANDARD


# ══ HELD OUT — the cross-provider sweep and the back-compat controls ══════════════════


def test_code_review_verify_does_not_resolve_cfg_model(bedrock_classes) -> None:
    """The DEFECT, pinned: Pass-2 must not follow `cfg.model`. Parameterised over the frontier id
    in both bare and provider-qualified form, because the rule this replaces compared for exact
    equality and so treated `anthropic:claude-opus-4-8` — the SAME model — as an explicit
    operator choice and kept Pass-2 on the frontier."""
    for frontier in ("claude-opus-4-8", "anthropic:claude-opus-4-8", _BEDROCK_FRONTIER):
        cfg = dataclasses.replace(LLMConfig(runner="fake"), model=frontier)
        resolved = _engine_resolved_model(_CODE_REVIEW_YAML, "verify", cfg)
        assert resolved == _BEDROCK_STANDARD, f"cfg.model={frontier} leaked into Pass-2"
        assert resolved != frontier


def test_no_verifier_path_resolves_a_foreign_provider(bedrock_classes) -> None:
    """AC: with classes configured for a non-default provider, NO verifier path resolves a model
    belonging to a different provider.

    The point of enumerating all three in ONE test is that they are independent code paths that
    have already drifted apart once: plan-review's `_verifier_cfg`, the completion verifier's
    private `_verifier_model_for_completion`, and code review's YAML step. A cutover that moves
    two of the three still bills the third to direct Anthropic — or hard-fails in a Bedrock-only
    container with no Anthropic key.
    """
    from rebar.llm.completion import _verifier_model_for_completion
    from rebar.llm.plan_review import _verifier_cfg

    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-opus-4-8")
    resolved = {
        "plan-review verify/coach": _verifier_cfg(cfg).model,
        "completion verifier": _verifier_model_for_completion(),
        "code-review verify": _engine_resolved_model(_CODE_REVIEW_YAML, "verify", cfg),
    }
    for path, model in resolved.items():
        assert infer_provider(model) == "bedrock", f"{path} resolved {model!r}, not a bedrock model"
        assert model == _BEDROCK_STANDARD, f"{path} resolved {model!r}"


def test_nothing_configured_keeps_the_verifier_on_the_standard_default(monkeypatch) -> None:
    """The back-compat control. With no class configured, `standard` resolves to
    `VERIFIER_DEFAULT_MODEL` — the same MODEL the pre-172e rule picked — merely provider-qualified.
    Asserted against the constant rather than a hardcoded id so a future default change does not
    silently make this test a lie."""
    from rebar.llm.config import VERIFIER_DEFAULT_MODEL

    _set_class_table(monkeypatch, {})
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-opus-4-8")
    resolved = _engine_resolved_model(_CODE_REVIEW_YAML, "verify", cfg)
    assert resolved.endswith(VERIFIER_DEFAULT_MODEL)
    assert infer_provider(resolved) == infer_provider(VERIFIER_DEFAULT_MODEL)


def test_a_literal_step_model_still_round_trips_unchanged(bedrock_classes) -> None:
    """Reserved values must not swallow literal ids: every other gate step that names a concrete
    model keeps resolving byte-for-byte, which is what makes the three reserved words safe."""
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-opus-4-8")
    for literal in ("claude-opus-4-8", "anthropic:claude-opus-4-8", "openai:gpt-4o"):
        assert resolve_model(cfg, step=literal) == literal


def test_the_superseded_equality_rule_has_no_production_consumers() -> None:
    """AC: enumerate the consumers of `resolve_verifier_model` and assert none selects a verifier
    by equality against the default model.

    `review_kernel.verify.resolve_verifier_model` still carries the SUPERSEDED rule
    (`verifier_default if model == default_model else model`) and remains exported + documented in
    docs/review-kernel.md, so it is deliberately not deleted here. What must stay true is that
    NOTHING under src/rebar calls it: a new caller would silently reintroduce the exact defect
    172e removed, because provider-qualifying a model defeats the equality test. If this fails,
    either drop the new call site or finish retiring the helper — do not relax the assertion.
    """
    src = _REPO_ROOT / "src" / "rebar"
    callers = sorted(
        f"{module.relative}:{i}"
        for module in parsed_python_files(src)
        for i, line in enumerate(module.source.splitlines(), 1)
        if "resolve_verifier_model(" in line and "def resolve_verifier_model" not in line
    )
    assert callers == [], f"resolve_verifier_model called from: {callers}"
