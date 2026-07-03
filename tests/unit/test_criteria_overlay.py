"""Project-supplied criteria overlay — the MVP-root vocabulary + cache-isolation seam
(epic 3156, story ef7e).

These tests exercise the `.rebar/criteria_routing.json` OVERLAY that opens plan-review's
closed criterion vocabulary: `effective_criteria(repo_root)` (built-ins ∪ activated
project ids), `effective_routing(repo_root)` (packaged routing merged with the overlay,
repo-keyed so the long-lived MCP server never leaks one repo's routing into another), and
the load-time collision/rebind/namespace validation (each a LOCATED error, never a silent
skip).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm import prompt_library
from rebar.llm.plan_review import registry

_PROJECT_RUBRIC = """\
---
schema_version: 1
title: No bare print() in library code
description: Project invariant — library code must not call print().
execution_mode: single_turn
category: plan-review-criterion
dimension: project-invariants
---
Flag any plan that introduces a bare print() call in importable library code (use logging).
"""

_ROUTING = {
    "exec": "1-TURN",
    "facet": "project-invariants",
    "applies_at": {"scope": ["container", "leaf"]},
    "block_threshold": 0.9,
    "default_posture": "advisory",
    "checklist": [],
}


def _make_repo(
    tmp_path: Path, *, overlay: dict | None, prompts: dict[str, str] | None = None
) -> str:
    """Materialize a project root with an optional overlay + project criterion prompts."""
    root = tmp_path
    if overlay is not None:
        rebar_dir = root / ".rebar"
        rebar_dir.mkdir(parents=True, exist_ok=True)
        (rebar_dir / "criteria_routing.json").write_text(json.dumps(overlay), encoding="utf-8")
    for pid, body in (prompts or {}).items():
        pdir = root / ".rebar" / "prompts"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"{pid}.md").write_text(body, encoding="utf-8")
    return str(root)


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    yield
    prompt_library._invalidate_caches()


# ── vocabulary opening ──────────────────────────────────────────────────────────
def test_overlay_absent_is_packaged_only(tmp_path):
    """No overlay ⇒ EXACTLY the canonical built-in vocabulary (no behaviour change)."""
    root = _make_repo(tmp_path, overlay=None)
    assert set(registry.effective_criteria(root)) == set(registry.CANONICAL_LLM)


def test_activated_project_criterion_opens_the_vocabulary(tmp_path):
    root = _make_repo(
        tmp_path,
        overlay={"plan_review": {"project.no-print": _ROUTING}, "activate": ["project.no-print"]},
        prompts={"plan-review-project-no-print": _PROJECT_RUBRIC},
    )
    eff = registry.effective_criteria(root)
    assert "project.no-print" in eff
    # built-ins are still all present
    assert set(registry.CANONICAL_LLM) <= set(eff)
    # and it loads a descriptor from its prompt file + overlay routing
    descriptors = {c["id"]: c for c in registry.load_criteria(root)}
    assert "project.no-print" in descriptors
    assert descriptors["project.no-print"]["block_threshold"] == 0.9
    assert descriptors["project.no-print"]["name"]  # from the prompt front-matter title


def test_present_but_not_activated_does_not_run(tmp_path):
    """Presence in the overlay ≠ active — a project id absent from `activate` is loaded into
    routing but is NOT in the effective (runnable) vocabulary."""
    root = _make_repo(
        tmp_path,
        overlay={"plan_review": {"project.no-print": _ROUTING}, "activate": []},
        prompts={"plan-review-project-no-print": _PROJECT_RUBRIC},
    )
    assert "project.no-print" not in registry.effective_criteria(root)
    # ...but its routing is still merged (available for tooling/eval)
    assert "project.no-print" in registry.effective_routing(root)


def test_retune_builtin_merges_over_packaged(tmp_path):
    """An un-prefixed built-in id in the overlay RE-TUNES it (per-key merge), keeping the
    other packaged routing keys."""
    root = _make_repo(tmp_path, overlay={"plan_review": {"F1": {"block_threshold": 0.5}}})
    routing = registry.effective_routing(root)
    assert routing["F1"]["block_threshold"] == 0.5
    # untouched keys survive the merge
    assert "facet" in routing["F1"] and "applies_at" in routing["F1"]


# ── load-time validation (located errors) ───────────────────────────────────────
def test_netnew_unprefixed_id_rejected(tmp_path):
    root = _make_repo(tmp_path, overlay={"plan_review": {"myrule": _ROUTING}, "activate": []})
    with pytest.raises(registry.RegistryError, match="must be 'project.<name>'-prefixed"):
        registry.effective_routing(root)


def test_project_id_colliding_with_builtin_rejected(tmp_path, monkeypatch):
    """A `project.`-prefixed id that equals a built-in id is rejected at load — a project
    criterion can NEVER rebind a built-in. Normally unreachable (built-in ids are never
    `project.`-prefixed), so we simulate a would-be future built-in named with a dot to
    prove the defensive guard fires."""
    monkeypatch.setattr(registry, "CANONICAL_LLM", registry.CANONICAL_LLM | {"project.evil"})
    prompt_library._invalidate_caches()
    root = _make_repo(tmp_path, overlay={"plan_review": {"project.evil": _ROUTING}, "activate": []})
    with pytest.raises(registry.RegistryError, match="collides with a built-in"):
        registry.effective_routing(root)


def test_activated_without_routing_rejected(tmp_path):
    root = _make_repo(tmp_path, overlay={"plan_review": {}, "activate": ["project.ghost"]})
    with pytest.raises(registry.RegistryError, match="has no.*routing entry"):
        registry.effective_criteria(root)


def test_activate_of_non_project_id_rejected(tmp_path):
    root = _make_repo(tmp_path, overlay={"plan_review": {}, "activate": ["totally-made-up"]})
    with pytest.raises(registry.RegistryError, match="must be a 'project.<name>'"):
        registry.effective_criteria(root)


def test_malformed_overlay_is_located_error(tmp_path):
    rebar_dir = tmp_path / ".rebar"
    rebar_dir.mkdir(parents=True)
    (rebar_dir / "criteria_routing.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(registry.RegistryError, match="not valid JSON"):
        registry.effective_routing(str(tmp_path))


def test_invalid_routing_entry_rejected(tmp_path):
    bad = {**_ROUTING, "block_threshold": 5.0}
    root = _make_repo(tmp_path, overlay={"plan_review": {"project.x": bad}, "activate": []})
    with pytest.raises(registry.RegistryError, match="block_threshold"):
        registry.effective_routing(root)


def test_legacy_levels_rejected_with_migration_hint(tmp_path):
    # Proportionate scrutiny is keyed on container/leaf; a stale overlay using the old
    # ticket-type `levels` vocabulary must fail loudly, not be silently ignored.
    bad = {**_ROUTING, "applies_at": {"levels": ["task"]}}
    root = _make_repo(tmp_path, overlay={"plan_review": {"project.x": bad}, "activate": []})
    with pytest.raises(registry.RegistryError, match="no longer supported.*scope"):
        registry.effective_routing(root)


def test_invalid_scope_value_rejected(tmp_path):
    bad = {**_ROUTING, "applies_at": {"scope": ["task"]}}  # 'task' is a type, not a node
    root = _make_repo(tmp_path, overlay={"plan_review": {"project.x": bad}, "activate": []})
    with pytest.raises(registry.RegistryError, match="scope must be"):
        registry.effective_routing(root)


# ── cache isolation: NO cross-repo routing leakage (the G6 RED test) ─────────────
def test_no_cross_repo_leakage(tmp_path):
    """A long-lived process resolving criteria for repo A (with an overlay) then repo B
    (without) must NOT serve A's project criterion for B. Proves the overlay-merged views
    are repo-keyed, not globally cached."""
    repo_a = _make_repo(
        tmp_path / "a",
        overlay={"plan_review": {"project.only-a": _ROUTING}, "activate": ["project.only-a"]},
        prompts={"plan-review-project-only-a": _PROJECT_RUBRIC},
    )
    repo_b = _make_repo(tmp_path / "b", overlay=None)

    # Resolve A first (populates the repo-keyed memo), then B.
    assert "project.only-a" in registry.effective_criteria(repo_a)
    assert "project.only-a" not in registry.effective_criteria(repo_b)
    # ...and the reverse order too (memo isolation is symmetric)
    prompt_library._invalidate_caches()
    assert "project.only-a" not in registry.effective_criteria(repo_b)
    assert "project.only-a" in registry.effective_criteria(repo_a)


def test_activated_missing_prompt_fails_loud(tmp_path):
    """An activated project criterion whose rubric prompt file is ABSENT surfaces a located
    RegistryError from load_criteria — fail-loud, never a silent skip."""
    root = _make_repo(
        tmp_path,
        overlay={"plan_review": {"project.no-prompt": _ROUTING}, "activate": ["project.no-prompt"]},
        prompts={},  # no prompt file authored for project.no-prompt
    )
    assert "project.no-prompt" in registry.effective_criteria(root)  # activated + routed
    with pytest.raises(registry.RegistryError, match="project.no-prompt"):
        registry.load_criteria(root)


# ── Pass-1 fan-in: an activated project criterion routes into the finder set ──────
def test_route_criteria_fans_in_activated_project_criterion(tmp_path):
    """route_criteria (the source of the ProductionBatchRunner's project fan-in) returns an
    activated project LLM criterion in its single-turn set — the deterministic proxy for
    'fans out and runs in Pass-1' without a billable LLM call."""
    from rebar.llm.plan_review import production_batch_runner
    from rebar.llm.plan_review.det_floor import PlanContext
    from rebar.llm.plan_review.orchestrator import route_criteria

    root = _make_repo(
        tmp_path,
        overlay={"plan_review": {"project.no-print": _ROUTING}, "activate": ["project.no-print"]},
        prompts={"plan-review-project-no-print": _PROJECT_RUBRIC},
    )
    ctx = PlanContext(
        ticket_id="abcd-0000-0000-0001",
        ticket_type="task",
        title="A task",
        description="## Acceptance Criteria\n- [ ] do the thing\n" + "x" * 200,
        repo_root=root,
    )
    single, agent = route_criteria(ctx)
    assert "project.no-print" in {c["id"] for c in single}
    # ...and the runner's project fan-in picks exactly the project subset (built-ins excluded)
    proj_single, proj_agent = production_batch_runner._project_criteria(ctx, exclude=set())
    assert {c["id"] for c in proj_single} == {"project.no-print"}
    assert proj_agent == []
    # deduped against an already-resolved built-in set is a no-op for project ids
    assert production_batch_runner._project_criteria(ctx, exclude={"project.no-print"}) == ([], [])


# ── MVP end-to-end: activate → runs through the runner → surfaces a finding ──────
def test_mvp_end_to_end_activated_project_criterion_surfaces_finding(tmp_path, monkeypatch):
    """The MVP slice: activate ONE project LLM criterion in the overlay → it fans in through
    ProductionBatchRunner → the finder (a FakeRunner, no billable call) runs it → its finding
    is surfaced in the batch result. Proves activate→run→surface end-to-end offline."""
    from rebar import config as _config
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner
    from rebar.llm.runner import FakeRunner
    from rebar.llm.workflow.runners import BatchRunRequest

    root = _make_repo(
        tmp_path,
        overlay={"plan_review": {"project.no-print": _ROUTING}, "activate": ["project.no-print"]},
        prompts={"plan-review-project-no-print": _PROJECT_RUBRIC},
    )
    # Overlay discovery keys off config.repo_root() (the lightweight context builder resolves
    # ctx.repo_root to None for a store-free tmp), so point it at the overlay repo.
    monkeypatch.setattr(_config, "repo_root", lambda *a, **k: tmp_path)
    prompt_library._invalidate_caches()

    state = {
        "ticket_id": "abcd-0000-0000-0001",
        "ticket_type": "story",
        "title": "Build X",
        "description": "## Why\nx\n\n## What\nbuild X\n\n## Acceptance Criteria\n- [ ] x is true\n",
        "deps": [],
    }
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, *, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda *, parent=None, repo_root=None: [])

    fake = FakeRunner(
        structured={
            "analysis": "",
            "findings": [
                {"finding": "bare print() in library code", "criteria": ["project.no-print"]}
            ],
        }
    )
    req = BatchRunRequest(
        finder="plan-review-finder",
        criteria=(),  # NO built-ins passed — the project criterion must be fanned in by the runner
        usd_budget=None,
        model_ladder=("claude-opus-4-8",),
        workflow={},
        target_ticket="abcd-0000-0000-0001",
        repo_root=str(root),
        run_id="run-1",
        step_id="finders",
    )
    result = ProductionBatchRunner(runner=fake).run(req, None)

    # the project criterion was fanned in...
    assert result.outputs["batch_plan"]["batch_resolution"]["project"] == ["project.no-print"]
    # ...ran, and surfaced its finding
    surfaced = [
        f for f in result.outputs["findings"] if "project.no-print" in (f.get("criteria") or [])
    ]
    assert surfaced, (
        f"expected the project criterion to surface a finding; got {result.outputs['findings']}"
    )


# ── packaged-routing parity gate (the CI drift gate) ─────────────────────────────
def test_packaged_routing_is_in_parity():
    """The committed criteria_routing.json is in parity with CANONICAL_LLM + structurally
    valid — the same check the CI parity gate runs (`registry validate-routing`)."""
    assert registry.validate_packaged_routing() == []


def test_editing_overlay_invalidates_memo_by_content(tmp_path):
    """The memo is keyed by overlay CONTENT signature, so editing the overlay is picked up
    without an explicit cache clear (no stale routing)."""
    root = _make_repo(
        tmp_path,
        overlay={"plan_review": {"project.p": _ROUTING}, "activate": []},
        prompts={"plan-review-project-p": _PROJECT_RUBRIC},
    )
    assert "project.p" not in registry.effective_criteria(root)
    # activate it by rewriting the overlay (new content ⇒ new signature ⇒ fresh compute)
    (Path(root) / ".rebar" / "criteria_routing.json").write_text(
        json.dumps({"plan_review": {"project.p": _ROUTING}, "activate": ["project.p"]}),
        encoding="utf-8",
    )
    assert "project.p" in registry.effective_criteria(root)
