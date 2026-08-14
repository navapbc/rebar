"""The completion verifier sets the agent step budget to a CRITERIA-SCALED floor (lever 1).

Completion verification is tool-heavy (potentially many criteria x several files each), so the
framework default ``max_iterations=250`` is the wrong budget in BOTH directions: too high for a
small ticket (a flat 480 floor used to MANUFACTURE the exhaustion the recovery path then banked
around - measured, an 8-criteria verify converges ~32 requests but spends the whole flat budget on
~77% read_file re-read waste until the runaway guard trips) and too low for a genuinely large one.
Epic 10ae / story 2948 lever 1 replaces the flat ``_VERIFY_MIN_STEPS = 480`` with a scaled floor;
ticket 8d74 recalibrates it: ``verify_step_floor(c, direct_children=k) = clamp(steps_per_criterion
x c + child_traversal x k + fixed_overhead, step_floor_min, 960)`` with defaults 24/16/16/160. The
budget exists ONLY to stop runaway tool use (runaway is separately guarded by tool_calls_limit +
loop detection); for valid tool use the floor is GENEROUS — child traversal and fixed overhead are
sized in, and the 960 clamp is a runaway ceiling, not a validation cap.

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

    def _fake(ticket_id, *, graph, repo_root, cfg, runner, verify_ref=None):
        captured["max_iterations"] = cfg.max_iterations
        return {"verdict": "PASS", "findings": [], "runner": "fake", "model": cfg.model}

    monkeypatch.setattr(gate_dispatch, "produce_completion_verdict", _fake)


def test_verify_step_floor_clamps_between_min_and_960() -> None:
    """The floor scales linearly in c (+ child term + overhead) and clamps to
    [step_floor_min, 960] — recalibrated defaults 24/criterion, min 160, ceiling 960."""
    vc = VerifyConfig()  # defaults: steps_per_criterion=24, step_floor_min=160
    assert verify_step_floor(1, vc) == 160  # 24 + 16 overhead < 160 -> clamped up to the min
    assert verify_step_floor(8, vc) == 208  # 24 x 8 + 16, inside the band
    assert verify_step_floor(40, vc) == 960  # 24 x 40 + 16 = 976 -> clamped to the ceiling
    assert verify_step_floor(100, vc) == _VERIFY_STEP_FLOOR_MAX == 960  # clamped down to the max
    assert verify_step_floor(0, vc) == 160  # degenerate zero-criteria surface floored at 1


def test_verify_step_floor_child_traversal_term() -> None:
    """A childful ticket sizes strictly larger than a childless one at equal criteria count:
    the child-traversal term (16 steps/direct child) lands INSIDE the formula."""
    vc = VerifyConfig()
    childless = verify_step_floor(8, vc)
    childful = verify_step_floor(8, vc, direct_children=4)
    assert childful == 272  # 24 x 8 + 16 x 4 + 16
    assert childful > childless == 208
    # Negative child counts are floored at 0, and the child term still respects the ceiling.
    assert verify_step_floor(8, vc, direct_children=-3) == childless
    assert verify_step_floor(40, vc, direct_children=50) == 960


def test_epic_request_budgets_meet_the_recalibrated_floor() -> None:
    """Sizing ACs (ticket 8d74): a 6-AC epic sizes to >=80 requests, a 15-AC epic to >=180
    (requests = steps / 2 as build_usage_limits halves them)."""
    vc = VerifyConfig()
    assert verify_step_floor(6, vc) / 2 >= 80
    assert verify_step_floor(15, vc) / 2 >= 180


def test_small_ticket_budget_is_lowered_below_the_framework_default(
    rebar_repo: Path, monkeypatch
) -> None:
    """A 1-criterion ticket at the framework default gets the scaled floor (160), NOT the old 480 -
    lever 1 lowers a small ticket below the 250 default rather than raising it to a flat 480."""
    r = str(rebar_repo)
    tid = _seed(rebar_repo, n_criteria=1)
    cfg = LLMConfig.from_env(repo_root=r)
    assert cfg.max_iterations == DEFAULT_MAX_ITERATIONS == 250

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == 160  # verify_step_floor(1) - lowered from 250


def test_large_ticket_budget_is_raised_above_the_framework_default(
    rebar_repo: Path, monkeypatch
) -> None:
    """A ticket with enough criteria that 24xc exceeds the 250 default is RAISED to the scaled
    floor (still capped at 960)."""
    r = str(rebar_repo)
    tid = _seed(rebar_repo, n_criteria=40)  # 24 x 40 + 16 = 976 -> clamped to 960
    cfg = LLMConfig.from_env(repo_root=r)

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == 960


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
    tid = _seed(rebar_repo, n_criteria=40)  # floor = 960 (clamped)
    # 100 != DEFAULT_MAX_ITERATIONS (an explicit operator budget) and 100 < 960 -> raised to 960.
    cfg = replace(LLMConfig.from_env(repo_root=r), max_iterations=100)

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == 960


def test_childful_ticket_sizes_larger_than_childless_at_equal_criteria(
    rebar_repo: Path, monkeypatch
) -> None:
    """Primary sizing counts DIRECT children from the ticket graph: a parent with two children
    receives the child-traversal term on top of the childless floor."""
    r = str(rebar_repo)
    childless = _seed(rebar_repo, n_criteria=8)
    parent = _seed(rebar_repo, n_criteria=8)
    for _ in range(2):
        rebar.create_ticket("task", "child", parent=parent, description="x" * 60, repo_root=r)
    cfg = LLMConfig.from_env(repo_root=r)

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(childless, config=cfg, repo_root=r)
    childless_budget = captured["max_iterations"]
    rebar.llm.verify_completion(parent, config=cfg, repo_root=r)
    childful_budget = captured["max_iterations"]

    assert childless_budget == 208  # 24 x 8 + 16
    assert childful_budget == 240  # + 16 x 2 children
    assert childful_budget > childless_budget


def test_child_enumeration_failure_fails_open_to_childless(rebar_repo: Path, monkeypatch) -> None:
    """Budget sizing must not raise when child enumeration fails: it falls open to
    direct_children=0 (the childless floor), preserving the site's enumeration-must-not-raise
    invariant."""
    r = str(rebar_repo)
    tid = _seed(rebar_repo, n_criteria=8)
    cfg = LLMConfig.from_env(repo_root=r)

    def _boom(*args, **kwargs):
        raise RuntimeError("store read failed")

    monkeypatch.setattr("rebar._reads.list_tickets", _boom)
    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == 208  # childless floor; no raise
