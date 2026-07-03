"""Per-criterion eval runner + calibration view (story 55b8).

Offline: the `run_case` criterion arm is driven by a `FakeRunner` (no model), and the
`calibrate_criterion` metric math + the CLI error paths use an injected `solve` / a
monkeypatched spec loader — no billable call. The live path (a criterion run as its
Pass-1 finder) is exercised structurally; the model-backed run belongs to the eval CI.
"""

from __future__ import annotations

import pytest

from rebar.llm.errors import LLMError
from rebar.llm.evals import eval as _eval
from rebar.llm.evals import eval_solver
from rebar.llm.runner import FakeRunner

_PLAN = "## Acceptance Criteria\n- [ ] improve the thing somehow\n"


# ── run_case criterion arm (the closed 3-id dispatch is now open) ────────────────
def test_run_case_criterion_arm_fire_and_no_fire():
    """A plan-review criterion id (bare or `plan-review-<id>`) runs as its Pass-1 finder
    over `case['input']`; fire ⇔ non-empty findings."""
    fire = FakeRunner(
        structured={
            "analysis": "",
            "findings": [{"finding": "x", "criteria": ["F1"], "location": "AC"}],
        }
    )
    r = eval_solver.run_case("plan-review-F1", {"input": _PLAN}, runner=fire)
    assert len(r["findings"]) == 1
    # bare id resolves too, and an empty finder result is no-fire
    nofire = FakeRunner(structured={"analysis": "", "findings": []})
    assert eval_solver.run_case("F1", {"input": _PLAN}, runner=nofire)["findings"] == []


def test_run_case_unknown_non_criterion_still_raises():
    """A prompt id that is neither a known reviewer nor a criterion still raises (the closed
    set's guarantee is preserved for genuinely-unknown ids)."""
    with pytest.raises(ValueError, match="no eval solver"):
        eval_solver.run_case(
            "totally-not-a-thing", {"input": _PLAN}, runner=FakeRunner(structured={})
        )


# ── calibrate_criterion metric math (deterministic, injected solve) ──────────────
def _spec(cases):
    return {"dataset": cases}


def test_calibrate_metrics_math(monkeypatch):
    cases = [
        {"id": "f1", "expect": "finding", "input": "a"},
        {"id": "f2", "expect": "fail", "input": "b"},
        {"id": "n1", "expect": "pass", "input": "c"},
        {"id": "n2", "expect": "pass", "input": "d"},
        {"id": "axis", "expect": "high_validity", "input": "e"},  # skipped (not fire/no-fire)
    ]
    monkeypatch.setattr(_eval, "load_eval_spec", lambda pid, *, repo_root=None: _spec(cases))
    # A criterion that fires on f1 (correct) + n1 (false-accept), misses f2, passes n2.
    fires = {"f1", "n1"}
    solve = lambda pid, case: {"findings": [{"x": 1}] if case["id"] in fires else []}  # noqa: E731

    r = _eval.calibrate_criterion("project.test", solve=solve)
    assert (r["n_fire"], r["n_nofire"]) == (2, 2)  # axis case excluded from fire/no-fire metrics
    assert r["n_discrimination"] == 1  # ...but the discrimination case still RAN (executed live)
    assert r["recall"] == 0.5  # f1 fired, f2 missed
    assert r["false_accept"] == 0.5  # n1 fired, n2 did not
    assert r["agreement"] == 0.5  # f1 + n2 correct; f2 + n1 wrong
    assert -1.0 <= r["kappa"] <= 1.0
    assert r["prompt"] == "plan-review-project-test"


def test_calibrate_perfect_criterion_kappa_one(monkeypatch):
    cases = [
        {"id": "f1", "expect": "finding", "input": "a"},
        {"id": "n1", "expect": "pass", "input": "b"},
    ]
    monkeypatch.setattr(_eval, "load_eval_spec", lambda pid, *, repo_root=None: _spec(cases))
    solve = lambda pid, case: {"findings": [{"x": 1}] if case["expect"] == "finding" else []}  # noqa: E731
    r = _eval.calibrate_criterion("F1", solve=solve)
    assert r["recall"] == 1.0 and r["false_accept"] == 0.0
    assert r["agreement"] == 1.0 and r["kappa"] == 1.0


def test_calibrate_n_run_stability(monkeypatch):
    cases = [
        {"id": "f1", "expect": "finding", "input": "a"},
        {"id": "n1", "expect": "pass", "input": "b"},
    ]
    monkeypatch.setattr(_eval, "load_eval_spec", lambda pid, *, repo_root=None: _spec(cases))
    # f1 flaps fire,fire,no-fire over 3 runs → majority fire, stability 2/3; n1 always no-fire.
    seq = {"f1": iter([True, True, False])}

    def solve(pid, case):
        fire = next(seq["f1"]) if case["id"] == "f1" else False
        return {"findings": [{"x": 1}] if fire else []}

    r = _eval.calibrate_criterion("F1", runs=3, solve=solve)
    assert r["runs"] == 3
    f1 = next(c for c in r["cases"] if c["id"] == "f1")
    assert f1["observed_fire"] is True and abs(f1["stability"] - 2 / 3) < 1e-9
    assert r["stability_min"] == pytest.approx(2 / 3)


def test_calibrate_empty_dataset_raises(monkeypatch):
    # only axis cases → no fire/no-fire → EvalError
    monkeypatch.setattr(
        _eval,
        "load_eval_spec",
        lambda pid, *, repo_root=None: _spec([{"id": "a", "expect": "high_validity"}]),
    )
    with pytest.raises(LLMError, match="empty calibration dataset"):
        _eval.calibrate_criterion("F1", solve=lambda p, c: {"findings": []})


# ── CLI error paths ──────────────────────────────────────────────────────────────
def test_cli_unknown_criterion_errors():
    from rebar._cli._llm_commands import _criteria

    assert _criteria(["eval", "NOPE-not-a-criterion"]) == 1


def test_cli_missing_id_errors():
    from rebar._cli._llm_commands import _criteria

    assert _criteria(["eval", ""]) == 2


def test_cli_absent_fixture_errors():
    # F1 is a real criterion but ships no .rebar/evals fixture → clear error, no traceback.
    from rebar._cli._llm_commands import _criteria

    assert _criteria(["eval", "F1"]) == 1
