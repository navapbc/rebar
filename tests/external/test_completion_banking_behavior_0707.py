"""Live behavioral regression for completion-verifier criterion banking (bug 0707).

This eval deliberately exercises the real dense ticket that exposed the zero-bank loop.  The
ticket and repository are read-only inputs: ``verify_completion`` is called with the local source
handle, and this module never emits a sidecar or transitions tracker state.

Measurement and oracle are denominated in evidence RESPONSES — distinct model responses
(``ctx.run_step``) at which a governed repository tool actually executed — because that is the
unit the completion evidence policy enforces (``max_evidence_responses`` in
``src/rebar/llm/completion_tool_policy.py``).  Commit ``e1352535ace`` (ticket
``dd41-239d-6e09-4a86``) deliberately lets a batched response execute ALL of its governed
reads, so an executed-CALL denomination is unbounded per response and went stale (bug
``9507-4676-27af-4344``).  Raw executed calls stay in the trial payload as a diagnostic only.
"""

from __future__ import annotations

import subprocess
from functools import wraps
from pathlib import Path
from typing import Any

import _bank_observer
import _live_llm
import pytest

import rebar
import rebar.llm
from rebar.llm import completion as completion_module
from rebar.llm import completion_tool_policy as tool_policy
from rebar.llm import pai_tools
from rebar.llm.config import LLMConfig
from rebar.llm.workflow import completion_banking as banking
from rebar.llm.workflow.completion_recovery import CompletionRecoveryError

pytest.importorskip("pydantic_ai")

pytestmark = pytest.mark.external

_MODEL = "bedrock:us.anthropic.claude-sonnet-4-6"
#: The provider _MODEL pins. This module does NOT follow the arm's `standard` model class, so
#: its readiness must be asked about bedrock specifically — on an arm that resolves anything
#: else there is no AWS credential (the OIDC step is gated to the bedrock arm) and every cell
#: here would fail on a provider this arm never claimed to cover (bug 4f74).
_PINNED_PROVIDER = _MODEL.partition(":")[0]
_live_llm_ready = _live_llm.live_llm_ready(_PINNED_PROVIDER)
_skip = _live_llm.skip_unless_provider(_PINNED_PROVIDER)
_TRIALS = 3


def _pinned_config(repo: Path) -> LLMConfig:
    return LLMConfig(
        model=_MODEL,
        repo_path=str(repo),
        runner="pydantic_ai",
        temperature=0.0,
    )


def _observe_trial(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ticket_id: str,
    repo: Path,
    graph: bool,
) -> dict[str, Any]:
    writes: list[str] = []
    responses_at_first_write: list[int] = []
    evidence_calls = 0
    evidence_steps: set[int] = set()
    original_upsert = banking.CriterionBank.upsert
    original_filesystem_tools = pai_tools.filesystem_tools
    original_policy_wrap = tool_policy.wrap_completion_evidence_policy

    def counting_filesystem_tools(repo_path: str | None) -> list[Any]:
        tools = original_filesystem_tools(repo_path)
        counted: list[Any] = []
        for tool in tools:

            @wraps(tool)
            def wrapped(*args: Any, __tool: Any = tool, **kwargs: Any) -> Any:
                nonlocal evidence_calls
                evidence_calls += 1
                return __tool(*args, **kwargs)

            counted.append(wrapped)
        return counted

    observed_upsert = _bank_observer.make_observed_upsert(
        original_upsert, writes, responses_at_first_write, lambda: len(evidence_steps)
    )
    counting_policy_wrap = _bank_observer.make_response_counting_wrap(
        original_policy_wrap, evidence_steps
    )

    with monkeypatch.context() as trial_patch:
        trial_patch.setattr(banking.CriterionBank, "upsert", observed_upsert)
        trial_patch.setattr(pai_tools, "filesystem_tools", counting_filesystem_tools)
        # agent_call imports wrap_completion_evidence_policy function-locally at call time,
        # so patching the source module attribute installs the response counter per run.
        trial_patch.setattr(tool_policy, "wrap_completion_evidence_policy", counting_policy_wrap)
        trial_patch.setattr(
            completion_module,
            "_verifier_model_for_completion",
            lambda _repo_path: _MODEL,
        )
        try:
            verdict = rebar.llm.verify_completion(
                ticket_id,
                graph=graph,
                ref="HEAD",
                source="local",
                fetch=False,
                repo_root=str(repo),
                config=_pinned_config(repo),
            )
            return {
                "banked": len(set(writes)),
                "verdict": verdict.get("verdict"),
                "error": None,
                "responses_at_bank": responses_at_first_write,
                "evidence_responses": len(evidence_steps),
                "evidence_calls": evidence_calls,
            }
        except CompletionRecoveryError as exc:
            return {
                "banked": len(set(writes)),
                "verdict": None,
                "error": str(exc),
                "responses_at_bank": responses_at_first_write,
                "evidence_responses": len(evidence_steps),
                "evidence_calls": evidence_calls,
            }


def _bounded_bank_gaps(trial: dict[str, Any], expected_criteria: int) -> bool:
    return _bank_observer.bounded_bank_gaps(trial, expected_criteria)


def _dense_ticket(repo: Path) -> str:
    """Build a real, deliberately broad repository-verification workload.

    Each criterion is demonstrably true, but a broad search reaches the production hit cap and
    must be narrowed repeatedly.  This recreates the bounded-yet-always-promising evidence stream
    that exposed the missing finite bank transition without depending on mutable tracker history.
    """
    adapters = repo / "src" / "adapters"
    adapters.mkdir(parents=True)
    for index in range(240):
        (adapters / f"adapter_{index:03d}.py").write_text(
            f'"""Compatibility adapter shard {index}."""\n'
            "CANONICAL_EXIT_VOCABULARY = (0, 1, 2)\n"
            "LEGACY_EXIT_VOCABULARY = (3, 4, 75)\n"
            "USES_SHARED_TYPED_DISPOSITION = True\n"
            "PRESERVES_WORKFLOW_ENVIRONMENT_COMPATIBILITY = True\n",
            encoding="utf-8",
        )
    subprocess.run(["git", "-C", str(repo), "add", "src/adapters"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "add compatibility adapters"],
        check=True,
    )
    description = (
        "Verify this dense compatibility migration across every adapter shard.\n\n"
        "## Acceptance Criteria\n"
        "- [ ] Every adapter exposes only canonical exit vocabulary 0, 1, and 2.\n"
        "- [ ] Every adapter preserves legacy exit vocabulary 3, 4, and 75.\n"
        "- [ ] Every canonical and legacy route consumes one shared typed disposition.\n"
        "- [ ] Every adapter preserves old workflow and environment compatibility.\n"
        "- [ ] All 240 adapter shards satisfy the same dual-contract invariants.\n"
    )
    ticket_id = rebar.create_ticket(
        "story", "Dense compatibility verification", description=description, repo_root=str(repo)
    )
    rebar.transition(ticket_id, "open", "in_progress", repo_root=str(repo))
    return ticket_id


@_skip
def test_dense_completion_banks_and_terminates_in_two_of_three_trials(
    monkeypatch: pytest.MonkeyPatch, rebar_repo: Path
) -> None:
    """Final oracle: the dense production scenario commits progress and returns a verdict."""
    repo = rebar_repo
    ticket_id = _dense_ticket(repo)
    trials = [
        _observe_trial(monkeypatch, ticket_id=ticket_id, repo=repo, graph=True)
        for _ in range(_TRIALS)
    ]
    print(f"dense completion trials: {trials}")
    successes = [trial for trial in trials if _bounded_bank_gaps(trial, expected_criteria=5)]
    assert len(successes) >= 2, f"dense completion banking successes={len(successes)}/3: {trials}"


@_skip
def test_simple_completion_control_still_banks_every_criterion(
    monkeypatch: pytest.MonkeyPatch, rebar_repo: Path
) -> None:
    """Negative control: the prompt repair must preserve the already-working simple case."""
    repo = rebar_repo
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def subtract(a, b):\n    return a - b\n\n"
        "def multiply(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "calculator.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add calculator"], check=True)
    description = (
        "## Acceptance Criteria\n"
        "- [ ] `calculator.py` defines `add(a, b)` and returns their sum.\n"
        "- [ ] `calculator.py` defines `subtract(a, b)` and returns their difference.\n"
        "- [ ] `calculator.py` defines `multiply(a, b)` and returns their product.\n"
    )
    ticket_id = rebar.create_ticket(
        "task", "Simple completion banking control", description=description, repo_root=str(repo)
    )
    rebar.transition(ticket_id, "open", "in_progress", repo_root=str(repo))

    trial = _observe_trial(monkeypatch, ticket_id=ticket_id, repo=repo, graph=False)
    assert trial["banked"] == 3, trial
    assert trial["verdict"] == "PASS", trial
    assert trial["error"] is None, trial
