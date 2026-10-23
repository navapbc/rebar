"""Each code-review LLM pass declares a model class. Base uses frontier,
Round A and B set frontier inside their batch model_ladder, and verify and
coach use standard. A step-level model on a batch step parses but is ignored,
so tests pin declaration placement and Bedrock provider resolution.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE = REPO_ROOT / "src" / "rebar" / "llm" / "workflow" / "gates" / "code-review.yaml"

# Pass-1 finders run on the frontier class; the Pass-2 verifier and Pass-4 coach on standard.
_EXPECTED_CLASS = {
    "base": "frontier",
    "round_a": "frontier",
    "round_b": "frontier",
    "verify": "standard",
    "coach": "standard",
}
_BATCH_STEPS = ("round_a", "round_b")


def _steps() -> dict[str, dict]:
    doc = yaml.safe_load(_GATE.read_text(encoding="utf-8"))
    return {s["id"]: s for s in doc["steps"] if isinstance(s, dict) and "id" in s}


def _declared_class(step: dict) -> str | None:
    """The class this step actually declares AT THE KEY ITS RUNNER READS.

    For a batch step that is `batch.model_ladder[0]` — a step-level `model:` is never read. For a
    single-shot prompt step it is the step-level `model:`.
    """
    batch = step.get("batch")
    if isinstance(batch, dict):
        ladder = batch.get("model_ladder") or []
        return ladder[0] if ladder else None
    return step.get("model")


# ══ HAPPY PATH ════════════════════════════════════════════════════════════════════════


def test_every_llm_step_declares_the_expected_class() -> None:
    """The core outcome: all five LLM-calling steps name a class, and the right one."""
    steps = _steps()
    missing = [sid for sid in _EXPECTED_CLASS if sid not in steps]
    assert missing == [], f"step ids missing from the gate (renamed?): {missing}"

    wrong = {
        sid: (_declared_class(steps[sid]), want)
        for sid, want in _EXPECTED_CLASS.items()
        if _declared_class(steps[sid]) != want
    }
    assert wrong == {}, f"declared class != expected (got, want): {wrong}"


def test_the_batch_steps_declare_inside_batch_not_at_step_level() -> None:
    """Batch model_ladder must carry each Pass-1 class inside batch config.
    A step-level model key on a batch step is accepted by the schema but ignored
    by the batch runner.
    """
    steps = _steps()
    for sid in _BATCH_STEPS:
        step = steps[sid]
        assert isinstance(step.get("batch"), dict), f"{sid} is no longer a batch step"
        ladder = step["batch"].get("model_ladder")
        assert ladder, (
            f"{sid} declares no batch.model_ladder; a step-level model: would be silently ignored "
            "(interpreter builds BatchRunRequest from the batch: sub-dict only)"
        )
        assert "model" not in step, (
            f"{sid} carries a step-level `model:`, which the batch runner NEVER reads. Move it to "
            "batch.model_ladder or it is a no-op that reads as done."
        )


def test_each_declared_class_resolves_through_the_class_table() -> None:
    """Runtime resolution, not YAML tokens: every declared value must be a RESERVED CLASS NAME that
    `resolve_model_string` maps through the configured class table. A bare model id would resolve to
    itself and pin the gate to one provider, which is the defect this ticket removes."""
    from rebar.llm.model_classes import CLASS_NAMES

    steps = _steps()
    non_class = {
        sid: _declared_class(steps[sid])
        for sid in _EXPECTED_CLASS
        if _declared_class(steps[sid]) not in CLASS_NAMES
    }
    assert non_class == {}, (
        f"these declare a non-class value: {non_class}. A literal model id "
        "does not follow the run's provider."
    )


# ══ HELD OUT ══════════════════════════════════════════════════════════════════════════


def test_the_resolved_model_per_pass_follows_a_bedrock_configuration(monkeypatch, tmp_path) -> None:
    """THE CRITERION THAT MATTERS. Under a Bedrock class configuration, each pass must resolve to
    THAT provider's model for its class — Pass-1 to the frontier entry, verifier/coach to standard.
    This is what no single `REBAR_LLM_MODEL` could express."""
    cfg_file = tmp_path / "classes.toml"
    cfg_file.write_text(
        "[llm.model_classes]\n"
        'frontier = { model = "bedrock:us.anthropic.claude-opus-4-8" }\n'
        'standard = { model = "bedrock:us.anthropic.claude-sonnet-4-6" }\n'
        'trivial  = { model = "bedrock:us.anthropic.claude-haiku-4-5" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(cfg_file))
    from rebar.llm.model_classes import resolve_model_string

    steps = _steps()
    want = {
        "frontier": "bedrock:us.anthropic.claude-opus-4-8",
        "standard": "bedrock:us.anthropic.claude-sonnet-4-6",
    }
    for sid, cls in _EXPECTED_CLASS.items():
        resolved = resolve_model_string(_declared_class(steps[sid]) or "")
        assert resolved == want[cls], f"{sid} ({cls}) resolved to {resolved!r}, want {want[cls]!r}"


def test_no_pass_resolves_to_anthropic_when_configured_for_bedrock(monkeypatch, tmp_path) -> None:
    """During a Bedrock review, every pass must resolve to Bedrock. A bare
    Claude id would select direct Anthropic and split the gate across providers.
    """
    cfg_file = tmp_path / "classes.toml"
    cfg_file.write_text(
        "[llm.model_classes]\n"
        'frontier = { model = "bedrock:us.anthropic.claude-opus-4-8" }\n'
        'standard = { model = "bedrock:us.anthropic.claude-sonnet-4-6" }\n'
        'trivial  = { model = "bedrock:us.anthropic.claude-haiku-4-5" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(cfg_file))
    from rebar.llm.model_classes import resolve_model_string

    steps = _steps()
    leaks = {}
    for sid in _EXPECTED_CLASS:
        resolved = resolve_model_string(_declared_class(steps[sid]) or "")
        if not resolved.startswith("bedrock:"):
            leaks[sid] = resolved
    assert leaks == {}, f"these passes did not stay on the configured provider: {leaks}"


def test_the_verifier_downgrade_still_applies_without_an_explicit_operator_model(
    monkeypatch, tmp_path
) -> None:
    """Pass-2/Pass-4 must stay on the cheaper `standard` class even when the operator has chosen
    nothing — the downgrade is a DEFAULT, not something only an explicit choice triggers. Asserted
    on a non-default provider, because that is where the old equality-based rule inverted."""
    cfg_file = tmp_path / "classes.toml"
    cfg_file.write_text(
        "[llm.model_classes]\n"
        'frontier = { model = "bedrock:us.anthropic.claude-opus-4-8" }\n'
        'standard = { model = "bedrock:us.anthropic.claude-sonnet-4-6" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(cfg_file))
    monkeypatch.delenv("REBAR_LLM_MODEL", raising=False)
    from rebar.llm.model_classes import resolve_model_string

    steps = _steps()
    verifier = resolve_model_string(_declared_class(steps["verify"]) or "")
    finder = resolve_model_string(_declared_class(steps["base"]) or "")
    assert verifier != finder, (
        "the verifier resolved to the same model as the Pass-1 finder, so the downgrade is not "
        f"applying (both {verifier!r})"
    )
    assert "sonnet" in verifier, (
        f"verifier should be the sonnet-family standard class, got {verifier!r}"
    )
    assert "opus" in finder, f"Pass-1 should be the opus-family frontier class, got {finder!r}"


def test_no_llm_step_is_left_without_a_declaration() -> None:
    """COMPLETENESS, so a step added later cannot quietly inherit `cfg.model`. Enumerates the gate's
    LLM-calling steps from the document itself rather than from the hardcoded table above — a new
    `prompt:`/`batch:` step with no class fails here even though `_EXPECTED_CLASS` omits it.
    """
    steps = _steps()
    undeclared = [
        sid
        for sid, s in steps.items()
        if ("prompt" in s or isinstance(s.get("batch"), dict)) and _declared_class(s) is None
    ]
    assert undeclared == [], (
        f"these LLM-calling steps declare no model class and will inherit cfg.model: {undeclared}"
    )


@pytest.mark.parametrize("sid", sorted(_BATCH_STEPS))
def test_the_batch_ladder_has_exactly_one_rung(sid: str) -> None:
    """A multi-rung ladder escalates per criterion, so the resolved model stops being
    the per-pass assertions above become unwritable. If escalation is wanted for cost, that is a
    separate change with a different oracle — not a quiet lengthening of this list."""
    ladder = (_steps()[sid].get("batch") or {}).get("model_ladder")
    assert ladder is not None, (
        f"{sid} declares no batch.model_ladder at all — see "
        "test_the_batch_steps_declare_inside_batch_not_at_step_level for why a step-level model: "
        "would not substitute"
    )
    assert len(ladder) == 1, (
        f"{sid}'s ladder has {len(ladder)} rungs {ladder}; one rung keeps the resolved model "
        "determinate so the per-pass criterion stays testable"
    )
