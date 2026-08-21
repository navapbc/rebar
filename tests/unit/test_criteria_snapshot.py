"""RP-06 S1 — the immutable effective review-policy snapshot (``CriteriaSnapshot``).

These are real producer→consumer contract tests: they feed a real project
``.rebar/criteria_routing.json`` overlay through the loader and the snapshot compiler,
then assert the exact selected policy, its source provenance, the deterministic digest,
and gate-specific applicability. One tier lower is insufficient because the defects this
snapshot removes occur BETWEEN the overlay producer and the packaged-routing consumers.

Applicability contract (the edge RP-06's plan-review flagged): for a ``code_review``
``project.``-prefixed LLM criterion, ``applies_to: ["**"]`` selects the criterion
UNCONDITIONALLY — including for a review whose ``changed_files`` set is empty — exactly
reproducing the prior ungated ``applies_to: []`` short-circuit, so migrating
``project.review-phase-boundaries`` from ``[]`` to ``["**"]`` never regresses at that edge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm import criteria
from rebar.llm.criteria import CriteriaSnapshot, compile_snapshot
from rebar.llm.prompting import prompt_library

_PLAN = "plan_review"
_CODE = "code_review"


# A project code-review LLM criterion (repository-wide) + a project code-review DET
# criterion + a plan-review project criterion (no applies_to) + a built-in retune and a
# built-in disable, so one overlay exercises every provenance/kind the snapshot compiles.
def _overlay(*, cr_globs) -> dict:
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
        for pid in ("house-style", "no-print"):
            (pdir / f"plan-review-project-{pid}.md").write_text("rubric", encoding="utf-8")
    return str(root)


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    criteria.clear_caches()
    yield
    prompt_library._invalidate_caches()
    criteria.clear_caches()


# ══════════════════════════════════════════════════════════════════════════════════
# HAPPY PATH (the implementer sees these) — pins the public snapshot API.
# ══════════════════════════════════════════════════════════════════════════════════
def test_compile_yields_digest_bound_view_with_provenance(tmp_path):
    """A real overlay compiled through ``CriteriaSnapshot`` yields ONE digest-bound view
    carrying effective built-in, project LLM, and project DET policy with source provenance."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["**"]))
    snap = compile_snapshot(repo_root=root)

    assert isinstance(snap, CriteriaSnapshot)
    # one deterministic digest (hex string), and the repo it is bound to
    assert isinstance(snap.digest, str) and len(snap.digest) == 64
    assert snap.repo_root == root

    # project LLM + project DET both selected on the code_review gate
    assert "project.house-style" in snap.project_llm(_CODE)
    assert "project.secret-shape" in snap.project_det(_CODE)
    # and both are in the active vocabulary
    assert "project.house-style" in snap.criteria(_CODE)
    assert "project.secret-shape" in snap.criteria(_CODE)

    # provenance: a project criterion is sourced from the overlay file; a built-in is packaged
    house = snap.record(_CODE, "project.house-style")
    assert house.kind == "project"
    assert house.exec == "1-TURN"
    assert house.source.endswith(".rebar/criteria_routing.json")
    det = snap.record(_CODE, "project.secret-shape")
    assert det.exec == "DET"
    # a packaged built-in exists on the plan_review gate with packaged provenance
    some_builtin = snap.builtins(_PLAN)[0]
    assert snap.record(_PLAN, some_builtin).kind == "builtin"
    assert snap.record(_PLAN, some_builtin).source == "packaged"


def test_repository_wide_glob_selects_changed_files(tmp_path):
    """A ``code_review`` project LLM criterion with ``applies_to: ["**"]`` selects a review
    that changed files."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["**"]))
    snap = compile_snapshot(repo_root=root)
    decision = snap.code_review_project_applies("project.house-style", ["src/x.py"])
    assert decision.applies is True
    assert decision.reason == "repository-wide"


# ══════════════════════════════════════════════════════════════════════════════════
# HELD-OUT: edge / negative-control / contract oracle (validated by the orchestrator).
# ══════════════════════════════════════════════════════════════════════════════════
def test_repository_wide_selects_on_empty_changed_files(tmp_path):
    """THE DIVERGING EDGE (RP-06 blocking finding T8): ``["**"]`` selects UNCONDITIONALLY
    even when ``changed_files`` is empty — reproducing the prior ungated ``[]`` short-circuit
    (``if not globs: return True``), which ``any(glob_match … over [] )`` would have flipped
    to False."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["**"]))
    snap = compile_snapshot(repo_root=root)
    decision = snap.code_review_project_applies("project.house-style", [])
    assert decision.applies is True
    assert decision.reason == "repository-wide"


def test_scoped_glob_nonmatch_records_non_applicable_reason(tmp_path):
    """A scoped glob that matches no changed path records a TYPED non-applicable reason and
    does not select (the criterion is never executed)."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["src/auth/**"]))
    snap = compile_snapshot(repo_root=root)
    decision = snap.code_review_project_applies("project.house-style", ["docs/readme.md"])
    assert decision.applies is False
    assert decision.reason == "no-changed-path-match"


def test_scoped_glob_matches_root_nested_and_dot_paths(tmp_path):
    """Given root, nested, and dot changed paths, a matching scoped glob selects; each path is
    admitted through the same real glob-match seam."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["**/x*"]))
    snap = compile_snapshot(repo_root=root)
    for changed in (["x.py"], ["a/b/x.py"], [".rebar/x.json"]):
        decision = snap.code_review_project_applies("project.house-style", changed)
        assert decision.applies is True, changed
        assert decision.reason.startswith("glob-match:")


@pytest.mark.parametrize("bad", [[], "**", [""], [123], None])
def test_empty_or_malformed_applies_to_rejected_for_code_review_project_llm(tmp_path, bad):
    """AC2: a ``code_review`` ``project.`` LLM criterion with an empty, blank, non-list, or
    (via ``None``) missing ``applies_to`` FAILS AT COMPILE (before any model call) with the
    criterion id, the configuration location, and the ``["**"]`` remedy."""
    overlay = _overlay(cr_globs=["**"])
    if bad is None:
        del overlay["code_review"]["project.house-style"]["applies_to"]
    else:
        overlay["code_review"]["project.house-style"]["applies_to"] = bad
    root = _make_repo(tmp_path, overlay)
    with pytest.raises(criteria.CriteriaError) as exc:
        compile_snapshot(repo_root=root)
    msg = str(exc.value)
    assert "project.house-style" in msg
    assert "criteria_routing.json" in msg
    assert '["**"]' in msg


def test_plan_review_project_criteria_do_not_adopt_code_review_glob_rule(tmp_path):
    """NEGATIVE CONTROL (AC6): a ``plan_review`` project criterion carries NO ``applies_to`` and
    still compiles — the stricter non-empty-glob rule is code-review-only, and the shared
    ``_validate_applies_to`` empty-list contract is left intact for plan review."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["**"]))
    snap = compile_snapshot(repo_root=root)  # must not raise
    assert "project.no-print" in snap.project_llm(_PLAN)


@pytest.mark.parametrize("det_applies_to", [[], None], ids=["empty-list", "missing"])
def test_code_review_project_det_is_exempt_from_nonempty_glob_rule(tmp_path, det_applies_to):
    """NEGATIVE CONTROL (AC2 scoping): the stricter NON-EMPTY-glob rule guards code-review
    project ``exec != DET`` (LLM) criteria ONLY — a ``code_review`` ``project.`` DET detector
    with an EMPTY or MISSING ``applies_to`` still COMPILES (deterministic detectors incur no
    model cost, so they are not gated on a non-empty applies_to), whereas the identical shapes
    are REJECTED for an LLM criterion (see the sibling LLM-rejection test)."""
    overlay = _overlay(cr_globs=["**"])
    if det_applies_to is None:
        del overlay["code_review"]["project.secret-shape"]["applies_to"]
    else:
        overlay["code_review"]["project.secret-shape"]["applies_to"] = det_applies_to
    root = _make_repo(tmp_path, overlay)
    snap = compile_snapshot(repo_root=root)  # must NOT raise for a DET criterion
    assert "project.secret-shape" in snap.project_det(_CODE)


@pytest.mark.parametrize(
    "malformed", ["**", [""], [123]], ids=["bare-string", "empty-glob", "non-string"]
)
def test_code_review_project_det_still_rejects_genuinely_malformed_applies_to(tmp_path, malformed):
    """The DET exemption is from the NON-EMPTY requirement only, not from basic shape validity:
    a genuinely malformed ``applies_to`` (non-list, empty-string glob, non-string element) is
    still rejected for a DET criterion by the shared tier-agnostic overlay validator."""
    overlay = _overlay(cr_globs=["**"])
    overlay["code_review"]["project.secret-shape"]["applies_to"] = malformed
    root = _make_repo(tmp_path, overlay)
    with pytest.raises(criteria.CriteriaError):
        compile_snapshot(repo_root=root)


def test_shared_select_ungated_branch_preserves_legacy_empty_list_meaning():
    """The shared ``select_project_applicability`` ``ungated`` branch (ADR 0102): empty or
    absent globs select UNCONDITIONALLY with the typed reason ``"ungated"`` — the legacy
    ``applies_to: []`` meaning that non-code-review consumers still rely on — and it selects
    even when ``changed_files`` is empty (the same edge the ``["**"]`` spelling must reproduce)."""
    from rebar.llm.criteria.snapshot import select_project_applicability

    for globs in ([], [""], [123]):
        decision = select_project_applicability(globs, ["any/file.py"])
        assert decision.applies is True
        assert decision.reason == "ungated"
    empty_edge = select_project_applicability([], [])
    assert empty_edge.applies is True
    assert empty_edge.reason == "ungated"


def test_snapshot_is_immutable(tmp_path):
    """Immutability: mutating a returned routing view never mutates the snapshot, and two
    reads return equal (independent) data."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["**"]))
    snap = compile_snapshot(repo_root=root)
    routing = snap.routing(_CODE)
    routing["project.house-style"] = {"tampered": True}
    routing.clear()
    assert "project.house-style" in snap.routing(_CODE)
    assert snap.routing(_CODE)["project.house-style"]["block_threshold"] == 0.8


def test_digest_is_deterministic_and_content_bound(tmp_path):
    """The digest is stable across recompiles of the same overlay and CHANGES when the overlay
    content changes (it reuses the overlay content signature)."""
    root_a = _make_repo(tmp_path / "a", _overlay(cr_globs=["**"]))
    d1 = compile_snapshot(repo_root=root_a).digest
    criteria.clear_caches()
    d2 = compile_snapshot(repo_root=root_a).digest
    assert d1 == d2
    root_b = _make_repo(tmp_path / "b", _overlay(cr_globs=["src/**"]))
    assert compile_snapshot(repo_root=root_b).digest != d1


def test_digest_is_a_cross_repo_content_fingerprint_not_a_repo_identifier(tmp_path):
    """ADR 0102's explicit cross-repo contract: the digest fingerprints the effective POLICY,
    not the repository, so two DISTINCT repositories whose effective overlays are byte-identical
    (or BOTH absent) share a digest, while a repo with different overlay content differs. Repo
    identity is carried separately by ``repo_root``, never by the digest."""
    same = _overlay(cr_globs=["**"])
    root_a = _make_repo(tmp_path / "ra", same)
    criteria.clear_caches()
    root_b = _make_repo(tmp_path / "rb", same)
    criteria.clear_caches()
    root_diff = _make_repo(tmp_path / "rc", _overlay(cr_globs=["src/**"]))
    criteria.clear_caches()
    root_absent_1 = _make_repo(tmp_path / "n1", None)
    criteria.clear_caches()
    root_absent_2 = _make_repo(tmp_path / "n2", None)

    criteria.clear_caches()
    dig_a = compile_snapshot(repo_root=root_a).digest
    criteria.clear_caches()
    dig_b = compile_snapshot(repo_root=root_b).digest
    criteria.clear_caches()
    dig_diff = compile_snapshot(repo_root=root_diff).digest
    criteria.clear_caches()
    dig_n1 = compile_snapshot(repo_root=root_absent_1).digest
    criteria.clear_caches()
    dig_n2 = compile_snapshot(repo_root=root_absent_2).digest

    assert dig_a == dig_b  # distinct repos, byte-identical overlay -> same policy digest
    assert dig_n1 == dig_n2  # distinct repos, both absent -> same policy digest
    assert dig_a != dig_diff  # different overlay content -> different digest
    # and the repo identity that the digest does NOT encode is carried by repo_root
    assert (
        compile_snapshot(repo_root=root_a).repo_root != compile_snapshot(repo_root=root_b).repo_root
    )


def test_effective_builtin_disable_present_in_snapshot(tmp_path):
    """AC7: a built-in the overlay DISABLES is absent from the active vocabulary yet recorded
    as disabled in the snapshot — proving effective built-in retunes/disables are captured."""
    # pick a real packaged plan-review built-in and disable it
    baseline = compile_snapshot(repo_root=None)
    victim = baseline.builtins(_PLAN)[0]
    overlay = _overlay(cr_globs=["**"])
    overlay.setdefault("plan_review", {})[victim] = {"disabled": True}
    root = _make_repo(tmp_path, overlay)
    snap = compile_snapshot(repo_root=root)
    assert victim in snap.disabled_builtins(_PLAN)
    assert victim not in snap.criteria(_PLAN)


def test_activated_project_det_present_in_snapshot(tmp_path):
    """AC8: an activated ``project.`` DET criterion appears in the snapshot's project-DET view."""
    root = _make_repo(tmp_path, _overlay(cr_globs=["**"]))
    snap = compile_snapshot(repo_root=root)
    assert "project.secret-shape" in snap.project_det(_CODE)
    assert snap.record(_CODE, "project.secret-shape").kind == "project"


def test_builtin_secondary_trigger_semantics_unchanged(tmp_path):
    """NEGATIVE CONTROL (AC9): overlay-absent, the snapshot's built-in vocabulary + routing for
    each gate is byte-identical to the packaged effective view (no snapshot-induced drift)."""
    snap = compile_snapshot(repo_root=None)
    for gate in (_PLAN, _CODE):
        assert set(snap.builtins(gate)) == set(criteria.effective_criteria(None, gate_key=gate))
        assert snap.routing(gate) == criteria.effective_routing(None, gate_key=gate)
