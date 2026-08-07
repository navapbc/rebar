"""The completion verifier sets the agent step budget to a CRITERIA-SCALED floor (lever 1).

Completion verification is tool-heavy (potentially many criteria x several files each), so the
framework default ``max_iterations=250`` is the wrong budget in BOTH directions: too high for a
small ticket (a flat 480 floor used to MANUFACTURE the exhaustion the recovery path then banked
around - measured, an 8-criteria verify converges ~32 requests but spends the whole flat budget on
~77% read_file re-read waste until the runaway guard trips) and too low for a genuinely large one.
Epic 10ae / story 2948 lever 1 replaces the flat ``_VERIFY_MIN_STEPS = 480`` with
``verify_step_floor(c) = clamp(steps_per_criterion x c, step_floor_min, 480)``:

* AUTHORITATIVE over the framework default - at ``max_iterations == DEFAULT_MAX_ITERATIONS`` the
  scaled floor becomes the budget even when that LOWERS it below 250 (a small ticket gets a
  proportionally smaller primary budget than the old flat 480).
* min-only against an EXPLICIT operator budget - a value the operator set via
  ``REBAR_LLM_MAX_STEPS`` is only ever RAISED up to the floor, never lowered.

Offline: spy on ``gate_dispatch.produce_completion_verdict`` (the delegate ``verify_completion``
hands the already-tuned cfg to) to capture the ``max_iterations`` - no workflow, no LLM call.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import rebar
import rebar.llm
from rebar._config_schema import VerifyConfig
from rebar.llm.completion import _VERIFY_STEP_FLOOR_MAX, verify_step_floor
from rebar.llm.config import DEFAULT_MAX_ITERATIONS, LLMConfig


def _seed(repo: Path, n_criteria: int = 1) -> str:
    checks = "\n".join(f"- [ ] criterion number {i} exists" for i in range(n_criteria))
    return rebar.create_ticket(
        "task",
        "Verify me",
        description=f"Body.\n\n## Acceptance Criteria\n{checks}\n",
        repo_root=str(repo),
    )


def _spy_produce(monkeypatch, captured: dict) -> None:
    from rebar.llm.workflow import gate_dispatch

    def _fake(ticket_id, *, graph, repo_root, cfg, runner):
        captured["max_iterations"] = cfg.max_iterations
        return {"verdict": "PASS", "findings": [], "runner": "fake", "model": cfg.model}

    monkeypatch.setattr(gate_dispatch, "produce_completion_verdict", _fake)


def test_verify_step_floor_clamps_between_min_and_480() -> None:
    """The floor scales linearly in c and clamps to [step_floor_min, 480]."""
    vc = VerifyConfig()  # defaults: steps_per_criterion=8, step_floor_min=48
    assert verify_step_floor(1, vc) == 48  # 8 < 48 -> clamped up to the min
    assert verify_step_floor(8, vc) == 64  # 8 x 8, inside the band
    assert verify_step_floor(40, vc) == 320  # 8 x 40, inside the band
    assert verify_step_floor(100, vc) == _VERIFY_STEP_FLOOR_MAX == 480  # clamped down to the max
    assert verify_step_floor(0, vc) == 48  # degenerate zero-criteria surface floored at 1


def test_small_ticket_budget_is_lowered_below_the_framework_default(
    rebar_repo: Path, monkeypatch
) -> None:
    """A 1-criterion ticket at the framework default gets the scaled floor (48), NOT the old 480 -
    lever 1 lowers a small ticket below the 250 default rather than raising it to a flat 480."""
    r = str(rebar_repo)
    tid = _seed(rebar_repo, n_criteria=1)
    cfg = LLMConfig.from_env(repo_root=r)
    assert cfg.max_iterations == DEFAULT_MAX_ITERATIONS == 250

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == 48  # verify_step_floor(1) - lowered from 250


def test_large_ticket_budget_is_raised_above_the_framework_default(
    rebar_repo: Path, monkeypatch
) -> None:
    """A ticket with enough criteria that 8xc exceeds the 250 default is RAISED to the scaled
    floor (still capped at 480)."""
    r = str(rebar_repo)
    tid = _seed(rebar_repo, n_criteria=40)  # 8 x 40 = 320 > 250
    cfg = LLMConfig.from_env(repo_root=r)

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == 320


def test_explicit_operator_higher_budget_wins(rebar_repo: Path, monkeypatch) -> None:
    """An explicit higher REBAR_LLM_MAX_STEPS is not lowered to the floor (min-only against an
    explicit budget)."""
    r = str(rebar_repo)
    tid = _seed(rebar_repo, n_criteria=1)
    cfg = replace(LLMConfig.from_env(repo_root=r), max_iterations=900)

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == 900


def test_explicit_operator_lower_budget_is_raised_to_floor(rebar_repo: Path, monkeypatch) -> None:
    """An explicit operator budget BELOW the scaled floor is raised up to it (the min-only arm)."""
    r = str(rebar_repo)
    tid = _seed(rebar_repo, n_criteria=40)  # floor = 320
    # 100 != DEFAULT_MAX_ITERATIONS (an explicit operator budget) and 100 < 320 -> raised to 320.
    cfg = replace(LLMConfig.from_env(repo_root=r), max_iterations=100)

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == 320
