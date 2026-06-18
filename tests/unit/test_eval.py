"""WS-G: prompt-eval discipline (rebar.llm.eval) — all offline, no model.

Covers G1 (spec loading + the packaged built-in), G2 (grader discipline, the
at_least(k) gate, coverage), and G3 (Cohen's kappa, judge alignment, JUnit).
"""

from __future__ import annotations

import pytest

from rebar.llm import eval as ev
from rebar.llm.eval import EvalError

# ── G1: spec loading + the packaged built-in ──────────────────────────────────


def test_packaged_code_quality_spec_loads_and_validates() -> None:
    spec = ev.load_eval_spec("code-quality")
    assert spec["prompt"] == "code-quality"
    assert spec["epochs"] == 3
    assert ev.validate_eval_spec(spec) == []


def test_user_spec_overrides_packaged(tmp_path) -> None:
    d = tmp_path / ".rebar" / "evals"
    d.mkdir(parents=True)
    (d / "code-quality.eval.yaml").write_text(
        "prompt: code-quality\nmodel: anthropic:claude-x\nepochs: 1\n"
        "gate: at_least(1)\ncoverage_threshold: 0.5\n"
        "scorers:\n  - {type: deterministic, name: ok}\n"
    )
    spec = ev.load_eval_spec("code-quality", repo_root=str(tmp_path))
    assert spec["epochs"] == 1  # the user spec, not the packaged one


def test_missing_spec_errors(tmp_path) -> None:
    with pytest.raises(EvalError, match="no eval spec"):
        ev.load_eval_spec("no-such-prompt", repo_root=str(tmp_path))


# ── G2: grader discipline ──────────────────────────────────────────────────────


def test_llm_judge_requires_pinned_cross_family_grader() -> None:
    bad = {"type": "llm-judge", "name": "j", "threshold": 0.7, "grader": {"model": "openai:gpt-4o"}}
    errs = ev.validate_scorer(bad, generator_model="openai:gpt-4o")
    blob = " ".join(errs)
    assert "temperature: 0" in blob
    assert "seed" in blob
    assert "snapshot" in blob
    assert "same family" in blob  # cross-family rule (both openai)


def test_llm_judge_must_not_gate() -> None:
    s = {
        "type": "llm-judge",
        "name": "j",
        "gates": True,
        "threshold": 0.7,
        "grader": {"model": "openai:gpt-4o", "temperature": 0, "seed": 1, "snapshot": "2024"},
    }
    assert any("must not gate" in e for e in ev.validate_scorer(s, generator_model="anthropic:c"))


def test_well_formed_cross_family_judge_passes() -> None:
    s = {
        "type": "llm-judge",
        "name": "j",
        "gates": False,
        "threshold": 0.7,
        "pointwise": True,
        "grader": {
            "model": "openai:gpt-4o-2024-08-06",
            "temperature": 0,
            "seed": 1,
            "snapshot": "2024-08-06",
        },
    }
    assert ev.validate_scorer(s, generator_model="anthropic:claude-opus-4-8") == []


def test_deterministic_scorer_simple() -> None:
    assert ev.validate_scorer({"type": "deterministic", "name": "x"}) == []
    assert ev.validate_scorer({"type": "deterministic"}) == ["deterministic scorer needs a `name`"]


def test_gate_rejects_pass_at_k() -> None:
    for bad in ("pass_at(0.9)", "pass@1", "pass_k(2)"):
        with pytest.raises(EvalError):
            ev.parse_gate(bad)
    assert ev.parse_gate("at_least(2)") == 2


def test_spec_requires_a_deterministic_gating_scorer() -> None:
    spec = {
        "prompt": "p",
        "model": "anthropic:c",
        "epochs": 2,
        "gate": "at_least(1)",
        "coverage_threshold": 0.8,
        "scorers": [
            {
                "type": "llm-judge",
                "name": "j",
                "gates": False,
                "threshold": 0.7,
                "grader": {"model": "openai:gpt-4o", "temperature": 0, "seed": 1, "snapshot": "x"},
            }
        ],
    }
    assert any("DETERMINISTIC" in e for e in ev.validate_eval_spec(spec))


def test_spec_requires_explicit_epochs() -> None:
    spec = {
        "prompt": "p",
        "gate": "at_least(1)",
        "coverage_threshold": 0.8,
        "scorers": [{"type": "deterministic", "name": "x"}],
    }
    assert any("epochs" in e for e in ev.validate_eval_spec(spec))


def test_at_least_passes_and_coverage() -> None:
    assert ev.at_least_passes([True, False, True], 2) is True
    assert ev.at_least_passes([True, False, False], 2) is False
    assert ev.coverage(8, 10) == 0.8
    assert ev.coverage_ok({"coverage_threshold": 0.8}, 8, 10) is True
    assert ev.coverage_ok({"coverage_threshold": 0.9}, 8, 10) is False


# ── G3: Cohen's kappa + judge alignment + JUnit ───────────────────────────────


def test_cohens_kappa_perfect_and_chance() -> None:
    assert ev.cohens_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0
    # total disagreement on a 2-category set → negative.
    assert ev.cohens_kappa(["a", "a", "b", "b"], ["b", "b", "a", "a"]) < 0
    assert ev.cohens_kappa([], []) == 1.0


def test_judge_alignment_gates_adoption() -> None:
    aligned = ev.judge_alignment(
        ["u", "n", "u"], ["u", "n", "u"], threshold=0.6, judge_snapshot="s1"
    )
    assert (
        aligned["aligned"] is True and aligned["kappa"] == 1.0 and aligned["judge_snapshot"] == "s1"
    )
    misaligned = ev.judge_alignment(["u", "u", "u", "u"], ["u", "n", "u", "n"], threshold=0.6)
    assert misaligned["aligned"] is False


def test_to_junit() -> None:
    xml = ev.to_junit(
        "code-quality",
        [
            {"name": "c1", "passed": True, "scorer": "det"},
            {"name": "c2", "passed": False, "message": "bad", "scorer": "det"},
        ],
    )
    assert 'tests="2"' in xml and 'failures="1"' in xml
    assert "<failure" in xml and "bad" in xml


def test_run_eval_guarded_without_extra() -> None:
    # Without inspect_ai, run_eval surfaces a clear, actionable error (not a crash).
    import importlib.util

    if importlib.util.find_spec("inspect_ai") is not None:
        pytest.skip("inspect_ai installed — guard path not exercised")
    with pytest.raises(EvalError):
        ev.run_eval("code-quality")


def test_cli_prompt_eval(capsys) -> None:
    import json

    from rebar._cli import main

    rc = main(["prompt", "eval", "code-quality", "--output", "json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is True
    assert report["prompt"] == "code-quality"
    assert "emits_valid_review_result" in report["gating_scorers"]
    assert report["gold_set_size"] == 3


def test_cli_prompt_eval_unknown(capsys) -> None:
    from rebar._cli import main

    rc = main(["prompt", "eval", "no-such-prompt-xyz"])
    assert rc == 1
    assert "no eval spec" in capsys.readouterr().err
