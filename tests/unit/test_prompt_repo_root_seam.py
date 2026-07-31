"""The criterion-discovery / prompt-resolution repo-root seam (bug dc05).

Criterion DISCOVERY resolves its root with ``criteria.overlay._resolve_repo_root`` (which
falls back to the ambient ``config.repo_root()``), while prompt RESOLUTION
(``prompting.prompts.get_prompt``) has NO fallback — a ``None`` root means "packaged prompts
only". Both rules are deliberate, so the seam between them must be CHECKED: when the root a
criterion was discovered from is not the root its rubric is resolved against, that is a wiring
bug and must fail loudly with an error naming both roots — never a misleading
``PromptNotFound`` claiming the id is unknown.

These tests pin, per the bug's acceptance criteria:

* the shared agreement check (equal roots pass; different roots — including
  ``root`` vs ``None`` — raise, naming both);
* the rewritten not-found message's three cases (no root supplied / root supplied but path
  absent / genuinely unknown id);
* ``get_prompt(repo_root=None)`` still meaning "packaged prompts only" (the hard constraint);
* the seam being enforced in BOTH gates — code-review's batch runner and plan-review's
  descriptor builder — and a client project's ``.rebar/prompts/<id>.md`` override resolving
  end to end in each.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
from rebar.llm.criteria import model as _criteria_model
from rebar.llm.criteria.overlay import (
    RepoRootMismatchError,
    _resolve_repo_root,
    check_repo_root_agreement,
)
from rebar.llm.plan_review import registry as plan_registry
from rebar.llm.prompting import prompt_library
from rebar.llm.prompting.prompts import PromptNotFound, get_prompt
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow.runners import BatchRunRequest

_UNKNOWN_ID = "dc05-definitely-not-a-prompt"

_CR_PROJECT_ID = "project.seam"
_CR_PROMPT_ID = "code-review-project-seam"
_CR_RUBRIC = """\
---
schema_version: 1
title: Project seam review
description: Project-owned code-review finder for the repo-root seam contract.
outputs: code_review_findings
execution_mode: agentic
category: code-review-pass
dimension: project-seam
---
Find seam violations in the supplied change.
"""

_PR_PROJECT_ID = "project.seam"
_PR_PROMPT_ID = "plan-review-project-seam"
_PR_RUBRIC = """\
---
schema_version: 1
title: Project seam plan review
description: Project-owned plan-review criterion for the repo-root seam contract.
execution_mode: single_turn
category: plan-review-criterion
dimension: project-invariants
---
Evaluate the project seam invariant.
"""
_PR_ROUTING = {
    "exec": "1-TURN",
    "facet": "project-invariants",
    "applies_at": {"scope": ["container", "leaf"]},
    "block_threshold": 0.9,
    "default_posture": "advisory",
    "checklist": [],
}


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    yield
    prompt_library._invalidate_caches()


def _write_prompt(root: Path, prompt_id: str, body: str) -> None:
    prompts = root / ".rebar" / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / f"{prompt_id}.md").write_text(body, encoding="utf-8")


# ── the shared agreement check ──────────────────────────────────────────────────────
def test_agreement_check_passes_when_both_roots_are_the_same(tmp_path) -> None:
    check_repo_root_agreement(str(tmp_path), str(tmp_path), where="unit")


def test_agreement_check_passes_when_both_roots_are_none(monkeypatch) -> None:
    """No resolvable root at all (library use, no checkout) — discovery and resolution
    BOTH degrade to packaged-only, so they agree."""
    monkeypatch.setattr(
        "rebar.config.repo_root", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no repo"))
    )
    assert _resolve_repo_root(None) is None
    check_repo_root_agreement(None, None, where="unit")


def test_agreement_check_raises_naming_both_roots(tmp_path) -> None:
    discovery = tmp_path / "discovered"
    resolution = tmp_path / "resolved"
    discovery.mkdir()
    resolution.mkdir()

    with pytest.raises(RepoRootMismatchError) as exc:
        check_repo_root_agreement(str(discovery), str(resolution), where="unit-seam")

    message = str(exc.value)
    assert str(discovery) in message
    assert str(resolution) in message
    assert "unit-seam" in message


def test_agreement_check_raises_when_resolution_root_is_none(tmp_path) -> None:
    """The exact shape of bug 2ea4: discovery activated a project criterion from a real root
    while resolution was handed ``None``, so project prompts were never searched."""
    with pytest.raises(RepoRootMismatchError) as exc:
        check_repo_root_agreement(str(tmp_path), None, where="unit-seam")

    assert str(tmp_path) in str(exc.value)


def test_agreement_check_resolves_the_discovery_root_fallback(tmp_path, monkeypatch) -> None:
    """Discovery's ``None`` means the AMBIENT root, so passing ``None`` for discovery and the
    ambient root for resolution AGREES — the check compares effective roots, not raw args."""
    monkeypatch.setattr("rebar.config.repo_root", lambda *a, **k: str(tmp_path))
    check_repo_root_agreement(None, str(tmp_path), where="unit")

    with pytest.raises(RepoRootMismatchError):
        check_repo_root_agreement(None, str(tmp_path / "elsewhere"), where="unit")


# ── the rewritten not-found message ─────────────────────────────────────────────────
def test_not_found_without_root_says_project_prompts_were_not_searched() -> None:
    with pytest.raises(PromptNotFound) as exc:
        get_prompt(_UNKNOWN_ID)

    message = str(exc.value)
    assert _UNKNOWN_ID in message
    # It must NOT claim the id is simply unknown — no root was supplied, so the project
    # override was never looked for. Say exactly that, and name what WOULD be searched.
    assert "not searched" in message
    assert ".rebar/prompts" in message


def test_not_found_with_root_names_the_exact_path_searched(tmp_path) -> None:
    with pytest.raises(PromptNotFound) as exc:
        get_prompt(_UNKNOWN_ID, repo_root=str(tmp_path))

    message = str(exc.value)
    searched = tmp_path / ".rebar" / "prompts" / f"{_UNKNOWN_ID}.md"
    assert str(searched) in message
    assert "not searched" not in message


def test_not_found_message_still_lists_the_known_builtins(tmp_path) -> None:
    """Case (c): a genuinely unknown id keeps the actionable built-in inventory."""
    for kwargs in ({}, {"repo_root": str(tmp_path)}):
        with pytest.raises(PromptNotFound) as exc:
            get_prompt(_UNKNOWN_ID, **kwargs)
        assert "code-review-base" in str(exc.value)


# ── the hard constraint: repo_root=None still resolves packaged prompts ─────────────
def test_packaged_prompt_resolves_with_no_repo_root() -> None:
    prompt = get_prompt("code-review-base")
    assert prompt.id == "code-review-base"
    assert prompt.text.strip()


def test_project_override_wins_over_the_packaged_prompt(tmp_path) -> None:
    _write_prompt(tmp_path, "code-review-base", _CR_RUBRIC)
    assert get_prompt("code-review-base", repo_root=str(tmp_path)).title == "Project seam review"
    assert get_prompt("code-review-base").title != "Project seam review"


# ── gate 1: code review ─────────────────────────────────────────────────────────────
class _RecordingAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, ctx):
        self.calls.append(ctx.step["prompt"])
        return _ex.StepResult(outputs={"findings": []})


def _batch_request(repo_root: str | None) -> BatchRunRequest:
    return BatchRunRequest(
        finder="code-review-base",
        criteria=(),
        usd_budget=None,
        model_ladder=(),
        workflow={},
        target_ticket=None,
        repo_root=repo_root,
        run_id="dc05-seam",
        step_id="round_a",
    )


def _cr_entries() -> tuple[dict[str, str], ...]:
    return ({"criterion_id": _CR_PROJECT_ID, "prompt": _CR_PROMPT_ID},)


def test_code_review_project_override_resolves_when_roots_agree(tmp_path) -> None:
    _write_prompt(tmp_path, _CR_PROMPT_ID, _CR_RUBRIC)
    agent = _RecordingAgent()
    runner = CodeReviewBatchRunner(
        context="DIFF",
        project_criteria=_cr_entries(),
        project_criteria_root=str(tmp_path),
    )

    runner.run(_batch_request(str(tmp_path)), agent)

    assert agent.calls == [_CR_PROMPT_ID]


def test_code_review_mismatched_roots_fail_loudly_not_as_prompt_not_found(tmp_path) -> None:
    discovery = tmp_path / "discovered"
    resolution = tmp_path / "resolved"
    _write_prompt(discovery, _CR_PROMPT_ID, _CR_RUBRIC)
    resolution.mkdir()
    runner = CodeReviewBatchRunner(
        context="DIFF",
        project_criteria=_cr_entries(),
        project_criteria_root=str(discovery),
    )

    with pytest.raises(RepoRootMismatchError) as exc:
        runner.run(_batch_request(str(resolution)), _RecordingAgent())

    message = str(exc.value)
    assert str(discovery) in message
    assert str(resolution) in message


def test_code_review_none_resolution_root_fails_loudly(tmp_path) -> None:
    """The regression bug 2ea4 hit: the criterion was discovered from a real root but the
    runner was handed ``None``, so the rubric silently fell through to 'unknown prompt'."""
    _write_prompt(tmp_path, _CR_PROMPT_ID, _CR_RUBRIC)
    runner = CodeReviewBatchRunner(
        context="DIFF",
        project_criteria=_cr_entries(),
        project_criteria_root=str(tmp_path),
    )

    with pytest.raises(RepoRootMismatchError):
        runner.run(_batch_request(None), _RecordingAgent())


# ── gate 2: plan review ─────────────────────────────────────────────────────────────
def _plan_repo(tmp_path: Path) -> str:
    rebar_dir = tmp_path / ".rebar"
    rebar_dir.mkdir(parents=True, exist_ok=True)
    (rebar_dir / "criteria_routing.json").write_text(
        json.dumps(
            {
                "plan_review": {_PR_PROJECT_ID: _PR_ROUTING},
                "activate": {_PR_PROJECT_ID: ["plan_review"]},
            }
        ),
        encoding="utf-8",
    )
    _write_prompt(tmp_path, _PR_PROMPT_ID, _PR_RUBRIC)
    return str(tmp_path)


def test_plan_review_project_override_resolves_for_an_activated_criterion(tmp_path) -> None:
    root = _plan_repo(tmp_path)

    assert _PR_PROJECT_ID in plan_registry.effective_criteria(root)
    descriptor = plan_registry.by_id(root)[_PR_PROJECT_ID]

    assert descriptor["scenario"] == "Evaluate the project seam invariant."
    assert descriptor["name"] == "Project seam plan review"


def test_plan_review_descriptor_seam_rejects_a_divergent_resolution_root(
    tmp_path, monkeypatch
) -> None:
    """The plan-review gate sits on the SAME seam: if the descriptor builder ever hands the
    prompt getter a root other than the one discovery resolved, that must fail loudly."""
    root = _plan_repo(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    real_build = _criteria_model.build_descriptor

    def _divergent(cid, routing_entry, *, repo_root=None, prompt_getter=None):
        return real_build(cid, routing_entry, repo_root=str(other), prompt_getter=prompt_getter)

    monkeypatch.setattr(
        "rebar.llm.plan_review.registry._criteria.build_descriptor", _divergent, raising=True
    )

    with pytest.raises(RepoRootMismatchError) as exc:
        plan_registry._descriptor_from_prompt(_PR_PROJECT_ID, repo_root=root)

    message = str(exc.value)
    assert root in message
    assert str(other) in message
