"""Unified criteria layer (story 5065) — the SHARED `rebar.llm.criteria` machinery both
review gates delegate to, and the properties the delegation must preserve.

Covers: (a) the gate-dispatched `threshold_for` divergence (the SAME criterion blocks under
one gate and not the other); (b) code-review gaining `.rebar/criteria_routing.json` overlay
support via its `code_review` gate key; (c) the exec-tier-polymorphic `build_descriptor`
(LLM-with-prompt vs prompt-less DET); (d) overlay-absent parity (both gates' effective_routing
== their packaged index); (e) plan-review's public registry functions still transparent under
delegation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm import criteria as shared
from rebar.llm import prompt_library
from rebar.llm.code_review import registry as cr
from rebar.llm.plan_review import registry as pr

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

_CR_ROUTING = {
    "exec": "1-TURN",
    "applies_to": [],
    "default_posture": "advisory",
    "block_threshold": 0.8,
    "blocking_enabled": False,
}


def _make_repo(tmp_path: Path, *, overlay: dict | None) -> str:
    root = tmp_path
    if overlay is not None:
        (root / ".rebar").mkdir(parents=True, exist_ok=True)
        (root / ".rebar" / "criteria_routing.json").write_text(
            json.dumps(overlay), encoding="utf-8"
        )
    return str(root)


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    yield
    prompt_library._invalidate_caches()


# ── (a) threshold_for gate-dispatch: the divergence is PRESERVED ────────────────────
def test_threshold_for_gate_dispatch_preserves_divergence():
    """The SAME criterion — `default_posture: "blocking"` but `blocking_enabled: false` —
    BLOCKS under gate="plan_review" (posture-derived) but NOT under gate="code_review"
    (explicit-flag-derived). The reverse case too: a flagged-only criterion blocks for
    code-review but not plan-review. This proves the two conventions live side-by-side."""
    posture_only = {
        "c": {"default_posture": "blocking", "blocking_enabled": False, "block_threshold": 0.9}
    }
    assert shared.threshold_for(["c"], posture_only, gate="plan_review") == (0.9, True)
    assert shared.threshold_for(["c"], posture_only, gate="code_review") == (0.9, False)

    flag_only = {
        "c": {"default_posture": "advisory", "blocking_enabled": True, "block_threshold": 0.7}
    }
    assert shared.threshold_for(["c"], flag_only, gate="plan_review") == (0.7, False)
    assert shared.threshold_for(["c"], flag_only, gate="code_review") == (0.7, True)


def test_threshold_for_min_and_unknown_default():
    """block_threshold = MIN over criteria; an unknown criterion contributes the default and
    is never blocking."""
    rmap = {"a": {"block_threshold": 0.5}, "b": {"block_threshold": 0.9}}
    assert shared.threshold_for(["a", "b"], rmap, gate="plan_review") == (0.5, False)
    assert shared.threshold_for(["unknown"], rmap, gate="code_review") == (
        shared.DEFAULT_BLOCK_THRESHOLD,
        False,
    )
    assert shared.threshold_for([], rmap, gate="plan_review") == (
        shared.DEFAULT_BLOCK_THRESHOLD,
        False,
    )


def test_threshold_for_unknown_gate_raises():
    with pytest.raises(shared.CriteriaError, match="unknown gate"):
        shared.threshold_for(["a"], {}, gate="nope")


# ── (b) code-review gains overlay support (its own gate key) ────────────────────────
def test_code_review_effective_criteria_picks_up_activated_project_criterion(tmp_path):
    """A `.rebar/criteria_routing.json` with a `code_review` map + `activate` opens the
    code-review vocabulary — the analog of plan-review's overlay, on the code-review gate key."""
    root = _make_repo(
        tmp_path,
        overlay={"code_review": {"project.no_eval": _CR_ROUTING}, "activate": ["project.no_eval"]},
    )
    eff = cr.effective_criteria(root)
    assert "project.no_eval" in eff
    # every packaged built-in overlay id is still present
    assert set(cr.routing_index()) <= set(eff)
    # its routing merged in, and threshold_for reads the re-tuned threshold via effective routing
    routing = cr.effective_routing(root)
    assert routing["project.no_eval"]["block_threshold"] == 0.8
    assert cr.threshold_for(["project.no_eval"], routing) == (0.8, False)


def test_code_review_overlay_is_isolated_from_plan_review_key(tmp_path):
    """A `code_review`-only overlay does NOT leak into plan-review's effective vocabulary and
    vice versa — each gate reads only its own key."""
    root = _make_repo(
        tmp_path,
        overlay={"code_review": {"project.cr_only": _CR_ROUTING}, "activate": ["project.cr_only"]},
    )
    assert "project.cr_only" in cr.effective_criteria(root)
    assert "project.cr_only" not in pr.effective_routing(root)
    assert "project.cr_only" not in pr.effective_criteria(root)


# ── (c) exec-tier-polymorphic build_descriptor ──────────────────────────────────────
def test_build_descriptor_llm_uses_prompt_getter():
    """A non-DET criterion resolves its rubric via the injected prompt_getter, merging the
    prompt's front-matter (title/dimension/body) with the routing entry."""

    class _Prompt:
        title = "My Rule"
        dimension = "project-invariants"
        text = "  the rubric body  "

    calls: list = []

    def _getter(cid, root):
        calls.append((cid, root))
        return _Prompt()

    routing = {"exec": "1-TURN", "block_threshold": 0.9, "default_posture": "advisory"}
    d = shared.build_descriptor("project.x", routing, repo_root="/repo", prompt_getter=_getter)
    assert calls == [("project.x", "/repo")]
    assert d["exec"] == "1-TURN"
    assert d["name"] == "My Rule"
    assert d["facet"] == "project-invariants"
    assert d["scenario"] == "the rubric body"  # stripped
    assert d["block_threshold"] == 0.9
    assert "fail_mode" not in d  # LLM descriptor carries no fail_mode


def test_build_descriptor_det_is_prompt_less():
    """An exec:DET criterion builds a PROMPT-LESS descriptor — the prompt_getter is NEVER
    called (a DET criterion is a detector, not an LLM rubric)."""

    def _getter(cid, root):  # pragma: no cover — must not be called
        raise AssertionError("prompt_getter must not be called for a DET criterion")

    routing = {
        "exec": "DET",
        "block_threshold": 0.5,
        "default_posture": "blocking",
        "fail_mode": "closed",
        "detector": {"id": "some.detector"},
    }
    d = shared.build_descriptor("project.det", routing, repo_root=None, prompt_getter=_getter)
    assert d["exec"] == "DET"
    assert d["checklist"] == []
    assert d["fail_mode"] == "closed"
    assert d["detector"] == {"id": "some.detector"}
    # scenario falls back to name/id when the detector suite is absent
    assert d["scenario"] == "project.det"


def test_build_descriptor_llm_without_getter_raises():
    with pytest.raises(shared.CriteriaError, match="no prompt_getter"):
        shared.build_descriptor("x", {"exec": "1-TURN"}, prompt_getter=None)


# ── (d) overlay-absent parity: both gates' effective_routing == packaged index ──────
def test_overlay_absent_parity_both_gates(tmp_path):
    root = _make_repo(tmp_path, overlay=None)
    assert pr.effective_routing(root) == pr._routing_index()
    assert cr.effective_routing(root) == cr.routing_index()
    assert set(pr.effective_criteria(root)) == set(pr.CANONICAL_LLM)
    assert set(cr.effective_criteria(root)) == set(cr.routing_index())


# ── (e) plan-review public functions still transparent under delegation ─────────────
def test_plan_review_public_functions_transparent(tmp_path):
    """Spot check: the delegating plan-review functions return exactly what the shared layer
    returns for the same gate key — the delegation is transparent."""
    root = _make_repo(
        tmp_path,
        overlay={"plan_review": {"F1": {"block_threshold": 0.5}}},
    )
    assert pr.effective_routing(root) == shared.effective_routing(root, gate_key="plan_review")
    assert pr.effective_criteria(root) == shared.effective_criteria(root, gate_key="plan_review")
    assert pr.disabled_builtins(root) == shared.disabled_builtins(root, gate_key="plan_review")
    # a re-tune lands, packaged keys survive the merge
    assert pr.effective_routing(root)["F1"]["block_threshold"] == 0.5
    assert "facet" in pr.effective_routing(root)["F1"]
