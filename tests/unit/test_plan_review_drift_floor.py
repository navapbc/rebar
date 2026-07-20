"""The Pass-3 DRIFT floor (bug 5e40) — convergent re-review on the plan-UNCHANGED + code-DRIFTED
axis. The tests pin: the pure drop predicate (drift-intersection, all quadrants), the in-place
verdict mutation over blocking+advisory (dropped→`dropped` bucket, drift-namespaced coverage,
corrected counts, BLOCK→PASS re-derivation), the eligibility candidate (plan-unchanged +
code-drifted; a MATERIAL edit must NOT be eligible), the entry gate, and — the guard — that the
whole-HEAD invalidation TRIGGER (compute_validity 'stale-head') is untouched. No live LLM: the
novelty map is injected.
"""

from __future__ import annotations

import types

import pytest

from rebar import config as core_config
from rebar.llm import plan_review
from rebar.llm.plan_review import attest, drift_floor, sidecar
from rebar.llm.plan_review.attest import compute_validity
from rebar.llm.review_kernel import decide

pytestmark = pytest.mark.unit

_T = 0.7
_DRIFTED = {"infra/autolander/loop.py"}


# ── pure drop predicate (drift-intersection) ─────────────────────────────────────────────────
def test_drift_floor_drop_quadrants() -> None:
    # novel + cites NO drifted file → DROP (converges to prior PASS)
    assert (
        decide.drift_floor_drop(
            0.9, cited_paths={"unrelated.py"}, drifted_files=_DRIFTED, t_novel=_T
        )
        is True
    )
    # novel + cites a drifted file → KEEP (a genuine code-drift signal)
    assert (
        decide.drift_floor_drop(
            0.9, cited_paths={"infra/autolander/loop.py"}, drifted_files=_DRIFTED, t_novel=_T
        )
        is False
    )
    # novel + no citations at all → DROP (it cannot touch drifted code)
    assert (
        decide.drift_floor_drop(0.9, cited_paths=set(), drifted_files=_DRIFTED, t_novel=_T) is True
    )
    # carryover (low novelty), even outside the drift set → KEEP (flagged before, must resolve)
    assert (
        decide.drift_floor_drop(
            0.3, cited_paths={"unrelated.py"}, drifted_files=_DRIFTED, t_novel=_T
        )
        is False
    )
    # boundary: novelty exactly T_novel is novel
    assert (
        decide.drift_floor_drop(
            0.7, cited_paths={"unrelated.py"}, drifted_files=_DRIFTED, t_novel=_T
        )
        is True
    )


# ── the in-place verdict mutation over blocking + advisory ────────────────────────────────────
def _blocked_verdict() -> dict:
    """A re-review that FLIPPED to BLOCK on drift: b0 is a NOVEL blocking finding citing an
    UNRELATED file (the 5e40 false positive), b1 is a NOVEL blocking finding citing the DRIFTED
    file (a genuine code-drift signal), a0 is NOVEL advisory noise citing an unrelated file,
    a1 is a carryover advisory."""
    return {
        "verdict": "BLOCK",
        "blocking": [
            {
                "id": "b0",
                "priority": 0.85,
                "criteria": ["E2"],
                "citations": [{"kind": "file", "path": "unrelated.py"}],
            },
            {
                "id": "b1",
                "priority": 0.85,
                "criteria": ["T3"],
                "citations": [{"kind": "file", "path": "infra/autolander/loop.py"}],
            },
        ],
        "advisory": [
            {
                "id": "a0",
                "priority": 0.2,
                "criteria": ["P5"],
                "citations": [{"kind": "file", "path": "docs/readme.md"}],
            },
            {
                "id": "a1",
                "priority": 0.2,
                "criteria": ["F1"],
                "citations": [{"kind": "file", "path": "unrelated.py"}],
            },
        ],
        "dropped": [],
        "coverage": {"counts": {"blocking": 2, "advisory_surfaced": 2, "dropped": 0}},
    }


def test_apply_drift_floor_drops_novel_outside_drift_and_converges_to_pass() -> None:
    v = _blocked_verdict()
    # combined index order [b0, b1, a0, a1]: b0/a0 novel-outside → drop; b1 novel-INSIDE-drift →
    # keep; a1 carryover → keep.
    novelty = {0: 0.95, 1: 0.95, 2: 0.9, 3: 0.1}
    drift_floor.apply_to_verdict(v, novelty, drifted_files=_DRIFTED, t_novel=_T)

    assert [f["id"] for f in v["blocking"]] == ["b1"]  # b0 dropped, b1 (cites drift) kept
    assert [f["id"] for f in v["advisory"]] == ["a1"]  # a0 dropped, a1 (carryover) kept
    assert sorted(f["id"] for f in v["dropped"]) == ["a0", "b0"]
    assert all(f["drop_reason"] == "drift" for f in v["dropped"])
    cov = v["coverage"]
    assert cov["narrowed"] is True and cov["drift_floor"] is True
    assert cov["drift_floored_finding_ids"] == ["b0", "a0"]
    assert cov["drifted_files"] == sorted(_DRIFTED)
    assert cov["counts"] == {"blocking": 1, "advisory_surfaced": 1, "dropped": 2}
    # b1 (a genuine drift finding) survives, so the verdict stays BLOCK — NOT a hollow PASS.
    assert v["verdict"] == "BLOCK"


def test_apply_drift_floor_flips_to_pass_when_only_novel_noise() -> None:
    """When EVERY novel finding is outside the drift set (pure non-determinism), the drift floor
    drops them all and the re-review converges to its prior PASS."""
    v = _blocked_verdict()
    v["blocking"] = [v["blocking"][0]]  # keep only b0 (novel, unrelated)
    v["coverage"]["counts"]["blocking"] = 1
    novelty = {0: 0.95, 1: 0.9, 2: 0.1}  # [b0, a0, a1]
    drift_floor.apply_to_verdict(v, novelty, drifted_files=_DRIFTED, t_novel=_T)
    assert v["blocking"] == []
    assert v["verdict"] == "PASS"  # BLOCK→PASS re-derivation


def test_apply_drift_floor_keeps_novel_inside_drift() -> None:
    """A NOVEL finding citing a drifted file MUST be kept (preserves code-drift detection)."""
    v = _blocked_verdict()
    v["blocking"] = [v["blocking"][1]]  # only b1 (novel, cites drifted file)
    v["advisory"] = []
    v["coverage"]["counts"] = {"blocking": 1, "advisory_surfaced": 0, "dropped": 0}
    drift_floor.apply_to_verdict(v, {0: 0.99}, drifted_files=_DRIFTED, t_novel=_T)
    assert [f["id"] for f in v["blocking"]] == ["b1"]
    assert v["dropped"] == []
    assert v["verdict"] == "BLOCK"
    assert "narrowed" not in v["coverage"]


def test_apply_drift_floor_unknown_drift_set_is_noop() -> None:
    """drifted_files=None (git diff unresolvable) → drop NOTHING (fail-safe: never suppress when
    we cannot prove which files drifted)."""
    v = _blocked_verdict()
    drift_floor.apply_to_verdict(
        v, {0: 0.99, 1: 0.99, 2: 0.99, 3: 0.99}, drifted_files=None, t_novel=_T
    )
    assert [f["id"] for f in v["blocking"]] == ["b0", "b1"]
    assert v["dropped"] == []
    assert v["verdict"] == "BLOCK"


# ── eligibility candidate ─────────────────────────────────────────────────────────────────────
def _patch_candidate(
    monkeypatch,
    *,
    verified=True,
    signed_material="pm",
    current_material="pm",
    signed_sha="shaA",
    current_sha="shaB",
    regver="rv1",
    cur_regver="rv1",
    prior_findings=(("f", "some prior finding"),),
    last_ts_offset_ns=0,
    drifted=("infra/autolander/loop.py",),
):
    from rebar import signing

    manifest = ["plan-review: PASS"]
    monkeypatch.setattr(
        signing,
        "verify_signature",
        lambda tid, repo_root=None: {"verified": verified, "manifest": manifest},
    )
    monkeypatch.setattr(attest, "is_plan_review_manifest", lambda m: True)
    monkeypatch.setattr(attest, "manifest_material", lambda m: signed_material)
    monkeypatch.setattr(
        attest, "current_material_fingerprint", lambda tid, repo_root=None: current_material
    )
    monkeypatch.setattr(signing, "verified_at_sha_from_manifest", lambda m: signed_sha)
    monkeypatch.setattr(signing, "head_sha", lambda repo_root: current_sha)
    monkeypatch.setattr(attest, "manifest_regver", lambda m: regver)
    monkeypatch.setattr(attest, "registry_version", lambda repo_root: cur_regver)
    monkeypatch.setattr(
        sidecar,
        "latest_review_result",
        lambda tid, repo_root=None: {"findings": [{"finding": t} for _, t in prior_findings]},
    )
    import time as _time

    monkeypatch.setattr(
        sidecar,
        "latest_review_timestamp",
        lambda tid, repo_root=None: _time.time_ns() - last_ts_offset_ns,
    )
    monkeypatch.setattr(
        drift_floor, "_drifted_paths", lambda base, head, repo_root=None: sorted(drifted)
    )


def test_candidate_eligible_when_plan_unchanged_and_code_drifted(monkeypatch) -> None:
    _patch_candidate(monkeypatch)
    cand = drift_floor.drift_floor_candidate("t", window_minutes=60)
    assert cand["eligible"] is True
    assert cand["reasons"]["plan_unchanged"] is True
    assert cand["reasons"]["code_drifted"] is True
    assert cand["drifted_files"] == ["infra/autolander/loop.py"]


def test_candidate_not_eligible_when_material_changed(monkeypatch) -> None:
    """GUARD (don't over-loosen): a plan whose MATERIAL changed is NOT drift-floor eligible — it
    must invalidate/re-review normally, never converge under the drift floor."""
    _patch_candidate(monkeypatch, current_material="pm-EDITED")
    cand = drift_floor.drift_floor_candidate("t", window_minutes=60)
    assert cand["reasons"]["plan_unchanged"] is False
    assert cand["eligible"] is False


def test_candidate_not_eligible_when_code_unchanged(monkeypatch) -> None:
    """No code drift (HEAD == signed) → not the drift regime → not eligible (that is the
    remediation axis, not this one)."""
    _patch_candidate(monkeypatch, current_sha="shaA")
    cand = drift_floor.drift_floor_candidate("t", window_minutes=60)
    assert cand["reasons"]["code_drifted"] is False
    assert cand["eligible"] is False


def test_candidate_not_eligible_when_registry_changed(monkeypatch) -> None:
    _patch_candidate(monkeypatch, cur_regver="rv2")
    cand = drift_floor.drift_floor_candidate("t", window_minutes=60)
    assert cand["reasons"]["registry_unchanged"] is False
    assert cand["eligible"] is False


def test_candidate_not_eligible_without_signature(monkeypatch) -> None:
    _patch_candidate(monkeypatch, verified=False)
    cand = drift_floor.drift_floor_candidate("t", window_minutes=60)
    assert cand["eligible"] is False and cand["drifted_files"] is None


# ── entry gate ────────────────────────────────────────────────────────────────────────────────
def _cfg():
    verify = types.SimpleNamespace(novelty_drop_threshold=_T, novelty_priority_floor=0.4)
    return types.SimpleNamespace(verify=verify)


def test_maybe_apply_inert_when_not_eligible(monkeypatch) -> None:
    monkeypatch.setattr(core_config, "load_config", lambda repo_root=None: _cfg())
    v = _blocked_verdict()
    drift_floor.maybe_apply("t", v, None, ctx=object(), cfg=_cfg(), runner=object(), repo_root=None)
    drift_floor.maybe_apply(
        "t", v, {"eligible": False}, ctx=object(), cfg=_cfg(), runner=object(), repo_root=None
    )
    assert v["dropped"] == [] and v["verdict"] == "BLOCK"


def test_maybe_apply_unknown_drift_set_inert(monkeypatch) -> None:
    monkeypatch.setattr(core_config, "load_config", lambda repo_root=None: _cfg())
    v = _blocked_verdict()
    drift_floor.maybe_apply(
        "t",
        v,
        {"eligible": True, "drifted_files": None},
        ctx=object(),
        cfg=_cfg(),
        runner=object(),
        repo_root=None,
    )
    assert v["dropped"] == []


def test_maybe_apply_floors_when_eligible(monkeypatch) -> None:
    monkeypatch.setattr(core_config, "load_config", lambda repo_root=None: _cfg())
    monkeypatch.setattr(
        sidecar,
        "latest_review_result",
        lambda tid, repo_root=None: {
            "findings": [{"id": "p", "finding": "x", "decision": "advisory"}]
        },
    )
    # Inject novelty: [b0, b1, a0, a1] → b0/a0 novel-outside dropped, b1 novel-inside kept.
    monkeypatch.setattr(
        plan_review,
        "_score_floor_novelty",
        lambda combined, prior, *, ctx, cfg, runner, repo_root: {0: 0.95, 1: 0.95, 2: 0.9, 3: 0.1},
    )
    v = _blocked_verdict()
    drift_floor.maybe_apply(
        "t",
        v,
        {"eligible": True, "drifted_files": list(_DRIFTED)},
        ctx=object(),
        cfg=_cfg(),
        runner=object(),
        repo_root=None,
    )
    assert sorted(f["id"] for f in v["dropped"]) == ["a0", "b0"]
    assert [f["id"] for f in v["blocking"]] == ["b1"]


# ── GUARD: the whole-HEAD invalidation TRIGGER is untouched ───────────────────────────────────
def test_whole_head_trigger_still_fires_on_unscoped_drift(monkeypatch) -> None:
    """The drift floor converges the re-review OUTCOME only; the whole-HEAD invalidation that
    TRIGGERS the re-review of an unscoped signed plan must be byte-unchanged — still 'stale-head'
    when HEAD drifts. This is the guard the abandoned fix (Gerrit 849) violated."""
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda tid, repo_root=None: "pm")
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv1")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headB")
    att = {
        "manifest": ["plan-review: PASS", "material: pm", "regver: rv1"],
        "head_sha": "headA",
        "signed_at": 100,
    }
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is False and res["verdict"] == "stale-head"
