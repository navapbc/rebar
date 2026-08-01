"""Every LLM-calling step in the code-review gate declares its model CLASS (ticket 3768).

One env var cannot express four passes. `code-review.yaml` used to declare a model on exactly ONE
of its five LLM-calling steps, so the other four inherited the run's single resolved `cfg.model` —
which is fine on the default Anthropic path and wrong everywhere else: a Bedrock run sent every
unclassed pass to whatever single model the operator named, losing the deliberate
opus-for-Pass-1 / sonnet-for-the-verifier split.

THE FIVE LLM STEPS, and the trap in how two of them must declare it:

    base     :67   Pass-1 finder, single-shot   step-level  `model: frontier`
    round_a  :77   Pass-1 finder, BATCH         inside batch:  `model_ladder: [frontier]`
    round_b  :109  Pass-1 finder, BATCH         inside batch:  `model_ladder: [frontier]`
    verify   :143  Pass-2 verifier              step-level  `model: standard`   (landed by 172e)
    coach    :182  Pass-4 coach                 step-level  `model: standard`

A STEP-LEVEL `model:` ON A BATCH STEP IS A SILENT NO-OP — this is the whole reason the batch rows
look different, and it is why this file asserts WHERE the declaration lives, not merely that one
exists. `interpreter.py` builds `BatchRunRequest` from the `batch:` sub-dict only
(`model_ladder=tuple(batch.get("model_ladder") or ())`) and never passes the enclosing step's
`model:`; `runners.py` computes `resolve_model_string(ladder[0]) if ladder else None`, whose
comment says an empty ladder means "no per-step model, NOT a resolved class". Because `model` IS a
legal step key (`schema.py`'s `_STEP_ORDER`), writing it on a batch step VALIDATES CLEANLY and is
then discarded. A test checking only "every LLM step names a class somewhere" would PASS while
two Pass-1 fan-outs silently kept inheriting `cfg.model`.

WHY ONE RUNG, NOT plan-review's `[trivial, standard, frontier]`: a multi-rung ladder escalates
criterion, so the model that answers a given criterion is not determinate — and this ticket's
criterion requires asserting THE resolved model per pass. One rung keeps that assertion writable.
Adopting escalation for cost is a separate change with a different oracle.

`resolve_verifier_model` is deliberately NOT involved. It still carries the superseded equality rule
but has zero callers under `src/rebar`, pinned by
`test_code_review_verifier_class.py::test_the_superseded_equality_rule_has_no_production_consumers`.
Adding a caller would reintroduce the defect 172e removed.
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
    """THE NO-OP GUARD, and the reason this file exists in this shape.

    `model:` at step level on a batch step parses fine and is then thrown away, so "fixing" these
    two steps that way would leave both Pass-1 fan-outs on `cfg.model` while every
    declaration-exists check still passed. Assert the declaration sits where the batch runner reads
    it, and that the decoy key is absent.
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
    """The cross-provider guard. A bare `claude-*` id infers provider `anthropic`, so if any pass
    resolved to a bare name the gate would send that pass to DIRECT ANTHROPIC during a Bedrock run —
    a split brain where the pass doing most of the work never reaches the provider under test."""
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
