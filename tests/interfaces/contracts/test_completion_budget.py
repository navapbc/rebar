"""The completion verifier raises the agent step budget to a FLOOR (``_VERIFY_MIN_STEPS``).

Completion verification is tool-heavy (potentially many criteria × several files each), so even
the raised framework default ``max_iterations=250`` is below its need → it would trip the
recursion cap mid-run, a false fail-closed block at the close gate. ``verify_completion`` floors
the budget to ``_VERIFY_MIN_STEPS`` (doubled to 480 after an 11-child framework epic tripped 240);
since 480 > the 250 default the floor still bites. An operator who explicitly sets a HIGHER
``REBAR_LLM_MAX_STEPS`` still wins.

Offline: spy on ``gate_dispatch.produce_completion_verdict`` (the delegate ``verify_completion``
hands the already-tuned cfg to) to capture the ``max_iterations`` — no workflow, no LLM call.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import rebar
import rebar.llm
from rebar.llm.completion import _VERIFY_MIN_STEPS
from rebar.llm.config import LLMConfig


def _seed(repo: Path) -> str:
    return rebar.create_ticket(
        "task",
        "Verify me",
        description="Body.\n\n## Acceptance Criteria\n- [ ] the thing exists\n",
        repo_root=str(repo),
    )


def _spy_produce(monkeypatch, captured: dict) -> None:
    from rebar.llm.workflow import gate_dispatch

    def _fake(ticket_id, *, graph, repo_root, cfg, runner):  # noqa: ANN001
        captured["max_iterations"] = cfg.max_iterations
        return {"verdict": "PASS", "findings": [], "runner": "fake", "model": cfg.model}

    monkeypatch.setattr(gate_dispatch, "produce_completion_verdict", _fake)


def test_verify_completion_floors_to_doubled_budget(rebar_repo: Path, monkeypatch) -> None:
    r = str(rebar_repo)
    tid = _seed(rebar_repo)
    cfg = LLMConfig.from_env(repo_root=r)
    assert cfg.max_iterations == 250  # the framework default we floor above (raised 50→250)
    assert _VERIFY_MIN_STEPS == 480  # the doubled completion-verifier floor

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == _VERIFY_MIN_STEPS


def test_verify_completion_operator_higher_budget_wins(rebar_repo: Path, monkeypatch) -> None:
    """An explicit higher REBAR_LLM_MAX_STEPS is not lowered to the floor."""
    r = str(rebar_repo)
    tid = _seed(rebar_repo)
    cfg = replace(LLMConfig.from_env(repo_root=r), max_iterations=900)

    captured: dict = {}
    _spy_produce(monkeypatch, captured)
    rebar.llm.verify_completion(tid, config=cfg, repo_root=r)

    assert captured["max_iterations"] == 900
