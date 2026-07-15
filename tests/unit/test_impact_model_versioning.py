"""Impact-model permissive rollout + versioning + calibration tooling (raptorial-galloping-dragon).

Covers the top-level `impact_model_version` tag on both REVIEW_RESULT sidecar payloads, the
version-segmented calibration replay (missing tag = skip), the permissive-rollout invariant (no
impact-graded criterion is a hard block), and the diff-grounded A/B gate.

Proving command:
    .venv/bin/pytest tests/unit/test_impact_model_versioning.py -v
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ── impact_model_version tag on both sidecar payloads ─────────────────────────────────────
def test_plan_review_payload_stamps_version() -> None:
    from rebar.llm.plan_review import sidecar

    payload = sidecar.build_payload({"verdict": "PASS", "ticket_id": "T1"}, material="m")
    assert payload["impact_model_version"] == sidecar.IMPACT_MODEL_VERSION == "plan-v2"


def test_code_review_payload_stamps_version() -> None:
    from rebar.llm.code_review import sidecar

    payload = sidecar.build_payload({"verdict": "PASS"}, target_ticket="A1")
    assert payload["impact_model_version"] == sidecar.IMPACT_MODEL_VERSION == "code-v3"


def test_plan_and_code_versions_are_distinct() -> None:
    from rebar.llm.code_review import sidecar as code_sc
    from rebar.llm.plan_review import sidecar as plan_sc

    assert plan_sc.IMPACT_MODEL_VERSION != code_sc.IMPACT_MODEL_VERSION


# ── version-segmented calibration replay ──────────────────────────────────────────────────
def _load_calib_module():
    path = Path("docs/experiments/calibrate_plan_review_thresholds.py")
    spec = importlib.util.spec_from_file_location("_calib_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_sidecar(dirp: Path, rid: str, ticket: str, version) -> None:
    d = dirp / rid
    d.mkdir(parents=True, exist_ok=True)
    data = {"ticket_id": ticket, "findings": [], "routing": {}}
    if version is not None:
        data["impact_model_version"] = version
    (d / f"{rid}-REVIEW_RESULT.json").write_text(json.dumps({"data": data}))


def test_replay_segments_by_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calib = _load_calib_module()
    tracker = tmp_path / ".tickets-tracker"
    _write_sidecar(tracker, "r-newA", "t1", "plan-v2")
    _write_sidecar(tracker, "r-newB", "t2", "plan-v2")
    _write_sidecar(tracker, "r-old", "t3", "plan-v1")
    _write_sidecar(tracker, "r-untagged", "t4", None)
    monkeypatch.chdir(tmp_path)

    # Segmented to plan-v2 → only the two v2 sidecars; v1 and untagged are skipped.
    seg, skipped = calib.load(impact_model_version="plan-v2")
    assert {t for t in seg} == {"t1", "t2"}
    # Untagged is NEVER pooled into a requested version.
    assert "t4" not in seg and "t3" not in seg
    # The excluded remainder is counted by reason, so a segmented run is auditable.
    assert skipped == {"different_version": 1, "untagged": 1, "unparseable": 0}
    # No version → pool all four (back-compat), nothing skipped.
    allrevs, skipped_all = calib.load()
    assert {t for t in allrevs} == {"t1", "t2", "t3", "t4"}
    assert sum(skipped_all.values()) == 0


# ── permissive-rollout invariant (no impact-graded hard block) ────────────────────────────
def test_code_review_approved_blocking_criteria_set() -> None:
    from rebar.llm.code_review import registry

    idx = registry.routing_index()
    blocking = {c for c, v in idx.items() if isinstance(v, dict) and v.get("blocking_enabled")}
    # The APPROVED hard-block set: the two pre-existing exec:DET security detectors PLUS the
    # `security` AGENT criterion that b9c0 (2026-07-12) flipped on at the 9f25-derived threshold
    # — the first LLM criterion permitted to block. Any addition beyond these three must be a
    # deliberate, re-approved change (this pin forces it).
    assert blocking == {"secret-detection", "high-critical-security", "security"}
    for c in ("secret-detection", "high-critical-security"):
        assert idx[c].get("exec") == "DET"
    # `security` is the deliberate exception to the old "only DET blocks" invariant.
    assert idx["security"].get("exec") == "AGENT" and idx["security"].get("block_threshold") == 0.54


# The plan-review criteria whose hard-block posture is APPROVED (set by the threshold-recalibration
# child #5 / commit 23581b171, "recalibrate criteria thresholds + postures"). Plan-review derives
# blocking from `default_posture == 'blocking'` (criteria/model.py), NOT `blocking_enabled` (absent
# here), so THIS is the meaningful permissive invariant: the impact redesign added no NEW blocking
# criterion. Adding one to the routing fails this test → forces a deliberate re-approval.
# Calibration 3 (task relishable-ammonitic-hoverfly) demoted T5e to advisory: the plan-v2
# segmented replay classed it FP-PRONE (validity 0.391, 59% verifier-drop, surviving p90
# priority 0.27) — see docs/experiments/plan-review-threshold-calibration.md "Calibration 3".
_PLAN_REVIEW_APPROVED_BLOCKING = frozenset(
    {"COH", "E2", "E4", "F1", "G1G2", "G5", "G6", "T1", "T4", "T8"}
)


def test_plan_review_blocking_posture_set_is_the_approved_set() -> None:
    routing = json.loads(Path("src/rebar/llm/plan_review/criteria_routing.json").read_text())
    blocking = {
        name
        for name, entry in routing.items()
        if isinstance(entry, dict) and entry.get("default_posture") == "blocking"
    }
    # Pin the blocking-posture set: no criterion became a NEW hard block via the impact redesign.
    assert blocking == set(_PLAN_REVIEW_APPROVED_BLOCKING)


# ── diff-grounded A/B gate ────────────────────────────────────────────────────────────────
def _load_ab_module():
    path = Path("docs/experiments/ab_impact_model.py")
    spec = importlib.util.spec_from_file_location("_ab_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_FIXTURE = "tests/unit/fixtures/code_review_impact_labels.jsonl"


def test_ab_absolute_gate_passes_for_current_model() -> None:
    ab = _load_ab_module()
    sep_new, nit_new, passes = ab.run_ab(_FIXTURE)
    # The current impact_code clears the ADR-0035 separation contract on the fixture (strict >).
    assert sep_new > ab.MIN_SEPARATION
    assert nit_new < ab.NIT_CEILING
    assert passes is True


def test_ab_gate_is_regression_detecting() -> None:
    # The gate is ABSOLUTE, so a DEGRADED model that fails to separate FAILS it — unlike a
    # "beat the old mean" gate, which the old mean (~0.25 flat) would let any non-zero pass.
    ab = _load_ab_module()
    rows = ab.load_labels(_FIXTURE)
    # A degenerate scorer that returns a constant → zero separation → gate MUST fail.
    _, _, sep_flat, passes_flat = ab.gate(rows, lambda _attrs: 0.5)
    assert sep_flat == 0.0 and passes_flat is False
    # The real model passes on the same rows.
    _, _, _, passes_real = ab.gate(rows)
    assert passes_real is True


def test_code_review_impact_model_version_is_v3() -> None:
    # f32e: adding a maint-lane binary is a vocabulary change → IMPACT_MODEL_VERSION bump.
    from rebar.llm.code_review import sidecar as code_sc

    assert code_sc.IMPACT_MODEL_VERSION == "code-v3"
