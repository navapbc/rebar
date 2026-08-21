"""RP-06 S6 — the workflow editor's read-only Effective Policy view.

These are real producer→consumer contract tests. They feed a real project
``.rebar/criteria_routing.json`` overlay through the S1 ``CriteriaSnapshot`` compiler and
assert the NARROW, read-only projection the editor server exposes at ``/effective-policy``:
snapshot ``digest``/``source`` plus, per criterion, its gate, execution ``tier``, effective
``posture``, ``applicability`` summary, and ``enabled``/provenance — and NOTHING else (no
discovery trace, no prompt/context body, no provider response, no secret). The projection is
configuration provenance, not a review-result viewer.

The companion fail-loud criterion-authoring rule (a ``project.*`` code-review LLM criterion
MUST declare non-empty ``applies_to`` globs, with plan-review/DET/built-in negative controls)
is pinned in ``test_editor_batch.py`` alongside the other criterion-authoring tests.

The implementer sees only the HAPPY-PATH section; the HELD-OUT section (edge/error/exclusion)
is validated by the orchestrator against code it could not tailor to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm import criteria
from rebar.llm.criteria import compile_snapshot
from rebar.llm.prompting import prompt_library
from rebar.llm.workflow import editor_contracts

_PLAN = "plan_review"
_CODE = "code_review"

# The read-only projection is provenance ONLY. These keys are the ENTIRE per-criterion
# contract — any key outside this set risks leaking review-result/debugging detail (AC7).
_ALLOWED_CRITERION_KEYS = {
    "id",
    "gate",
    "tier",
    "posture",
    "applicability",
    "enabled",
    "source",
    "reason",
}
# Substrings that would betray a leak of trace / prompt body / provider payload / secret.
_FORBIDDEN_SUBSTRINGS = (
    "prompt",
    "rubric",
    "checklist",
    "body",
    "context",
    "provider",
    "response",
    "completion",
    "trace",
    "token",
    "secret",
    "api_key",
    "detector",
)


def _overlay(*, cr_globs) -> dict:
    """One overlay exercising every provenance/kind the effective view projects: a project
    code-review LLM criterion (its ``applies_to`` is the variable under test), a project
    code-review DET criterion, a plan-review project criterion, and (implicitly) the packaged
    built-ins for both gates."""
    return {
        "code_review": {
            "project.house-style": {
                "exec": "1-TURN",
                "facet": "project-invariants",
                "applies_to": cr_globs,
                "block_threshold": 0.8,
                "default_posture": "advisory",
            },
            "project.secret-shape": {
                "exec": "DET",
                "facet": "project-invariants",
                "applies_to": ["**"],
                "detector": {"id_prefix": "project.secret."},
                "fail_mode": "open",
                "block_threshold": 0.9,
                "default_posture": "advisory",
            },
        },
        "plan_review": {
            "project.no-print": {
                "exec": "1-TURN",
                "facet": "project-invariants",
                "applies_at": {"scope": ["container", "leaf"]},
                "block_threshold": 0.9,
                "default_posture": "advisory",
                "checklist": [],
            },
        },
        "activate": {
            "project.house-style": ["code_review"],
            "project.secret-shape": ["code_review"],
            "project.no-print": ["plan_review"],
        },
    }


def _make_repo(tmp_path: Path, overlay: dict | None) -> str:
    root = tmp_path
    if overlay is not None:
        rebar_dir = root / ".rebar"
        rebar_dir.mkdir(parents=True, exist_ok=True)
        (rebar_dir / "criteria_routing.json").write_text(json.dumps(overlay), encoding="utf-8")
        pdir = rebar_dir / "prompts"
        pdir.mkdir(parents=True, exist_ok=True)
        for pid in ("code-review-project-house-style", "plan-review-project-no-print"):
            (pdir / f"{pid}.md").write_text("rubric", encoding="utf-8")
    return str(root)


def _by_id(view: dict) -> dict[str, dict]:
    return {c["id"]: c for c in view["criteria"]}


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    criteria.clear_caches()
    yield
    prompt_library._invalidate_caches()
    criteria.clear_caches()


# ══════════════════════════════════════════════════════════════════════════════════
# HAPPY PATH (the implementer sees these) — pins the read-only projection contract.
# ══════════════════════════════════════════════════════════════════════════════════
def test_effective_view_is_available_and_digest_bound(tmp_path):
    """A real overlay yields an AVAILABLE effective view carrying the snapshot's digest and a
    per-criterion projection of the compiled policy — the same digest the snapshot compiles."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["**"]))
    view = editor_contracts.effective_policy_view(repo_root=root)

    assert view["available"] is True
    assert view["unavailable_reason"] is None
    # digest-bound to the SAME snapshot the compiler produces (provenance, not a re-derivation)
    snap = compile_snapshot(repo_root=root)
    assert view["digest"] == snap.digest

    crit = _by_id(view)
    # the project LLM criterion is projected with gate/tier/posture/applicability/provenance
    house = crit["project.house-style"]
    assert house["gate"] == _CODE
    assert house["tier"] == "1-TURN"
    assert house["posture"] == "advisory"
    assert house["enabled"] is True
    assert house["applicability"] == "repository-wide"
    assert house["source"].endswith(".rebar/criteria_routing.json")
    # the project DET criterion is projected with its DET tier
    assert crit["project.secret-shape"]["tier"] == "DET"
    # a packaged built-in on the plan_review gate is projected with packaged provenance
    builtins = [c for c in view["criteria"] if c["gate"] == _PLAN and c["source"] == "packaged"]
    assert builtins, "expected at least one packaged plan-review built-in in the effective view"


# ══════════════════════════════════════════════════════════════════════════════════
# HELD-OUT: edge / error / exclusion oracle (orchestrator-validated).
# ══════════════════════════════════════════════════════════════════════════════════
def test_effective_view_reports_scoped_glob_applicability(tmp_path):
    """AC2: applicability provenance distinguishes a repository-wide criterion from a scoped
    one — a scoped glob is summarized as its glob, not as ``repository-wide``."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["src/**/*.py"]))
    view = editor_contracts.effective_policy_view(repo_root=root)
    house = _by_id(view)["project.house-style"]
    assert house["applicability"] != "repository-wide"
    assert "src/**/*.py" in house["applicability"]


def test_snapshot_failure_marks_the_view_unavailable_with_a_located_error(tmp_path):
    """AC5: a broken overlay (a code-review project LLM criterion with an illegal empty
    ``applies_to``) makes the effective view UNAVAILABLE with an actionable located message —
    it never silently substitutes packaged defaults as if they were effective policy."""
    root = _make_repo(tmp_path, _overlay(cr_globs=[]))
    view = editor_contracts.effective_policy_view(repo_root=root)

    assert view["available"] is False
    assert view["digest"] is None
    assert not view["criteria"]  # NO packaged-default substitution
    reason = view["unavailable_reason"]
    assert reason and "project.house-style" in reason  # located at the offending criterion
    assert 'use ["**"] for a repository-wide criterion' in reason  # actionable remedy


def test_effective_view_excludes_trace_prompt_provider_and_secret_material(tmp_path):
    """AC7: the projection is provenance ONLY — every criterion dict carries exactly the
    allowed provenance keys, and nothing anywhere in the payload leaks a stored discovery
    trace, a raw prompt/context body, a provider response, a detector body, or a secret."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["**"]))
    view = editor_contracts.effective_policy_view(repo_root=root)

    for crit in view["criteria"]:
        assert set(crit) <= _ALLOWED_CRITERION_KEYS, f"unexpected key(s): {set(crit)}"

    # No forbidden substring appears as a KEY anywhere in the serialized payload (a leaked
    # prompt body / provider response / detector config would surface as such a key).
    blob = json.dumps(view)

    def _keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from _keys(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from _keys(item)

    leaked = {k for k in _keys(view) for bad in _FORBIDDEN_SUBSTRINGS if bad in k.lower()}
    assert not leaked, f"effective view leaked debugging/secret keys: {leaked}"
    # and the raw rubric text placed in the overlay never rides along in the projection
    assert "detector" not in blob
