"""Tests for the Tier-0 Pass-3 replay harness (``rebar.llm.evals.plan_replay.tier0``,
ticket bouncy-peacockish-titmouse / 5d19-52e0-7c26-47fb).

Pure decision-replay functions are tested directly with plain finding dicts (fast,
precise -- this is where the harness's fidelity to production actually lives: the
``execution_review`` recovery, the mirrored ``_threshold_for`` closure, the on-target
veto cohort restriction, and the self-check invariant that ``replayed-stored`` always
equals ``stored``). ``run_tier0`` integration is exercised against a REAL git tracker
(mirroring test_plan_replay_corpus.py's ``TrackerBuilder``), since that is where the
git-object-walk + corpus machinery actually runs.
"""

from __future__ import annotations

import json
import subprocess
import uuid as uuidlib
from pathlib import Path

import pytest

from rebar.llm.evals.plan_replay import report, tier0
from rebar.llm.evals.plan_replay.candidates import CANDIDATES, Candidate
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import material_fingerprint
from rebar.llm.plan_review.sidecar import SidecarReviewPhaseError


def _fp(ticket_id: str, description: str) -> str:
    ctx = PlanContext(
        ticket_id=ticket_id,
        ticket_type="story",
        title="T",
        description=description,
        state={"file_impact": []},
        children=[],
    )
    return material_fingerprint(ctx)


pytestmark = pytest.mark.unit

_TS_COUNTER = [1700000000000000000]


def _next_ts() -> int:
    _TS_COUNTER[0] += 1_000_000_000
    return _TS_COUNTER[0]


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


class TrackerBuilder:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        _run_git(path, "init", "-q")
        _run_git(path, "config", "user.email", "test@example.com")
        _run_git(path, "config", "user.name", "Test")

    def _write_event(self, ticket_id: str, ts: int, event_type: str, data: dict) -> None:
        d = self.path / ticket_id
        d.mkdir(parents=True, exist_ok=True)
        ev_uuid = str(uuidlib.UUID(int=ts % (2**128)))
        fname = f"{ts}-{ev_uuid}-{event_type}.json"
        (d / fname).write_text(json.dumps({"data": data}))

    def create(self, ticket_id: str, *, description: str, ts: int | None = None) -> int:
        ts = ts or _next_ts()
        self._write_event(
            ticket_id, ts, "CREATE", {"ticket_type": "story", "description": description}
        )
        _run_git(self.path, "add", "-A")
        _run_git(self.path, "commit", "-q", "-m", f"create {ticket_id}")
        return ts

    def review_result(self, ticket_id: str, *, data: dict, ts: int | None = None) -> int:
        ts = ts or _next_ts()
        self._write_event(ticket_id, ts, "REVIEW_RESULT", data)
        _run_git(self.path, "add", "-A")
        _run_git(self.path, "commit", "-q", "-m", f"review {ticket_id}")
        return ts


# ── fixture helpers ───────────────────────────────────────────────────────────────

_ALL_YES_BINARY = {
    "is_verifiable": "yes",
    "evidence_entails_finding": "yes",
    "path_reachable": "yes",
    "impact_follows_necessarily": "yes",
    "no_viable_alternative_explanation": "yes",
    "no_existing_mitigation": "yes",
    "severity_claim_justified": "yes",
    "cited_reference_accurate": "na",
    "claims_absence": "na",
    "absence_confirmed_in_context": "na",
    "current_state_satisfies_plan_goal": "na",
    "committed_work_relies_on_unbacked_claim": "na",
    "respects_artifact_altitude": "na",
    "asserted_capability_confirmed": "na",
}


def _verification(*, severity: str = "high", **binary_overrides) -> dict:
    """A verification dict yielding validity=1.0 (all graded binaries "yes") and
    impact_sev from a single non-override severity axis (``vague_directive``)."""
    binary = {**_ALL_YES_BINARY, **binary_overrides}
    return {
        "binary": binary,
        "severity_attributes": {"vague_directive": severity},
    }


def _finding(
    *,
    criteria: list[str],
    block_threshold: float,
    blocking_enabled: bool,
    decision: str,
    severity: str = "high",
    **binary_overrides,
) -> dict:
    """A persisted-shape finding: carries its own PERSISTED ``block_threshold``/
    ``blocking_enabled``/``verification``/``decision`` exactly as the sidecar would."""
    return {
        "id": "f1",
        "criteria": criteria,
        "block_threshold": block_threshold,
        "blocking_enabled": blocking_enabled,
        "verification": _verification(severity=severity, **binary_overrides),
        "decision": decision,
    }


# ── execution_review_for ─────────────────────────────────────────────────────────


def test_execution_review_for_planning_default():
    assert tier0.execution_review_for({}) is False
    assert tier0.execution_review_for({"review_phase": "planning"}) is False


def test_execution_review_for_execution_detected():
    assert tier0.execution_review_for({"review_phase": "execution", "priority_floor": 0.8}) is True


def test_execution_review_for_malformed_payload_defaults_to_planning():
    # Not a Mapping -> SidecarReviewPhaseError inside parse_review_phase_metadata.
    assert tier0.execution_review_for(None) is False  # type: ignore[arg-type]


def test_execution_review_for_invalid_phase_value_defaults_to_planning():
    from rebar.llm.plan_review import sidecar

    with pytest.raises(SidecarReviewPhaseError):
        sidecar.parse_review_phase_metadata({"review_phase": None})
    # tier0's wrapper must not propagate the error.
    assert tier0.execution_review_for({"review_phase": None}) is False


# ── replayed_stored_decision (the self-check invariant) ──────────────────────────


def test_replayed_stored_decision_matches_stored_for_blocking_finding():
    f = _finding(
        criteria=["E2"],
        block_threshold=0.6,
        blocking_enabled=True,
        decision="block",
        severity="high",
    )
    result = tier0.replayed_stored_decision(f, execution_review=False)
    assert result["decision"] == f["decision"] == "block"


def test_replayed_stored_decision_matches_stored_for_advisory_finding():
    f = _finding(criteria=["E1"], block_threshold=0.95, blocking_enabled=False, decision="advisory")
    result = tier0.replayed_stored_decision(f, execution_review=False)
    assert result["decision"] == f["decision"] == "advisory"


def test_replayed_stored_decision_matches_stored_for_execution_phase_drop():
    f = _finding(
        criteria=["E4"],
        block_threshold=0.6,
        blocking_enabled=True,
        decision="dropped",
        current_state_satisfies_plan_goal="yes",
    )
    result = tier0.replayed_stored_decision(f, execution_review=True)
    assert result["decision"] == "dropped"
    assert result["reason"] == "veto:plan-goal-satisfied"


def test_replayed_stored_decision_matches_stored_at_execution_review_bump():
    # bt persisted at 0.80 (the execution_review bump already applied at review time).
    f = _finding(
        criteria=["E2"],
        block_threshold=0.80,
        blocking_enabled=True,
        decision="advisory",
        severity="medium",
    )
    result = tier0.replayed_stored_decision(f, execution_review=True)
    assert result["decision"] == f["decision"]


# ── mirrored_threshold_for / candidate_decisions ──────────────────────────────────


def test_candidate_current_equals_live_baseline_for_every_finding():
    findings = [
        _finding(criteria=["E2"], block_threshold=0.6, blocking_enabled=True, decision="block"),
        _finding(
            criteria=["E1"], block_threshold=0.95, blocking_enabled=False, decision="advisory"
        ),
    ]
    verifs = tier0.verifs_from_findings(findings)
    live = tier0.live_baseline_decisions(findings, verifs, execution_review=False)
    cand = tier0.candidate_decisions(
        findings, verifs, CANDIDATES["current"], execution_review=False
    )
    assert [d["decision"] for d in live] == [d["decision"] for d in cand]


def test_candidate_overlay_flips_boundary_finding():
    # E1 is advisory/0.95 live; a candidate overlay makes it blocking/0.5 -- this
    # high-priority (1.0) finding should flip from advisory (live) to block (candidate).
    findings = [
        _finding(criteria=["E1"], block_threshold=0.95, blocking_enabled=False, decision="advisory")
    ]
    verifs = tier0.verifs_from_findings(findings)
    live = tier0.live_baseline_decisions(findings, verifs, execution_review=False)
    assert live[0]["decision"] == "advisory"

    overlay_candidate = Candidate(overlay={"E1": (0.5, True)})
    cand = tier0.candidate_decisions(findings, verifs, overlay_candidate, execution_review=False)
    assert cand[0]["decision"] == "block"


def test_prerequisite_consistency_special_case():
    resolver = tier0.mirrored_threshold_for(CANDIDATES["current"], execution_review=False)
    assert resolver(["prerequisite-consistency"]) == (0.60, True)


def test_execution_review_bump_applies_to_mirrored_resolver():
    resolver_planning = tier0.mirrored_threshold_for(CANDIDATES["current"], execution_review=False)
    resolver_execution = tier0.mirrored_threshold_for(CANDIDATES["current"], execution_review=True)
    bt_planning, blocking = resolver_planning(["E2"])
    bt_execution, blocking_exec = resolver_execution(["E2"])
    assert blocking is True and blocking_exec is True
    assert bt_execution == max(bt_planning, 0.80)


# ── flip_matrix ────────────────────────────────────────────────────────────────


def test_flip_matrix_self_check_mismatches_zero_by_construction():
    findings = [
        _finding(criteria=["E2"], block_threshold=0.6, blocking_enabled=True, decision="block"),
        _finding(
            criteria=["E1"], block_threshold=0.95, blocking_enabled=False, decision="advisory"
        ),
    ]
    verifs = tier0.verifs_from_findings(findings)
    row = {
        "ticket_id": "aaaa-bbbb-cccc-dddd",
        "review_event_uuid": "u1",
        "execution_review": False,
        "stored": [f["decision"] for f in findings],
        "replayed_stored": [
            tier0.replayed_stored_decision(f, execution_review=False)["decision"] for f in findings
        ],
        "live_baseline": [
            d["decision"]
            for d in tier0.live_baseline_decisions(findings, verifs, execution_review=False)
        ],
        "candidate": [
            d["decision"]
            for d in tier0.candidate_decisions(
                findings, verifs, CANDIDATES["current"], execution_review=False
            )
        ],
    }
    matrix = tier0.flip_matrix([row])
    assert matrix["self_check_mismatches"] == 0


def test_flip_matrix_registry_drift_detected_without_self_check_mismatch():
    # persisted bt (0.99) is HIGHER than the live registry's E2 threshold (0.6) -> the
    # finding was advisory at review time but live-baseline now blocks it (drift).
    f = _finding(
        criteria=["E2"],
        block_threshold=0.99,
        blocking_enabled=True,
        decision="advisory",
        severity="medium",
    )
    verifs = tier0.verifs_from_findings([f])
    live = tier0.live_baseline_decisions([f], verifs, execution_review=False)
    assert live[0]["decision"] == "block"  # live registry's 0.6 threshold is crossed

    row = {
        "ticket_id": "t1",
        "review_event_uuid": "u1",
        "execution_review": False,
        "stored": [f["decision"]],
        "replayed_stored": [tier0.replayed_stored_decision(f, execution_review=False)["decision"]],
        "live_baseline": [live[0]["decision"]],
        "candidate": [live[0]["decision"]],
    }
    matrix = tier0.flip_matrix([row])
    assert matrix["self_check_mismatches"] == 0
    assert matrix["registry_drift_flips"] == 1


def test_flip_matrix_friction_rate_and_relief_count_on_known_ratio():
    rows = [
        {
            "ticket_id": "t1",
            "review_event_uuid": "u1",
            "execution_review": False,
            "stored": ["advisory", "advisory"],
            "replayed_stored": ["advisory", "advisory"],
            "live_baseline": ["advisory", "block"],
            "candidate": ["block", "advisory"],
        }
    ]
    matrix = tier0.flip_matrix(rows)
    assert matrix["total_findings"] == 2
    assert matrix["candidate_newly_blocking"] == 1  # index 0: advisory -> block
    assert matrix["relief_count"] == 1  # index 1: block -> advisory
    assert matrix["friction_rate"] == 0.5


def test_flip_matrix_empty_rows():
    matrix = tier0.flip_matrix([])
    assert matrix == {
        "total_findings": 0,
        "self_check_mismatches": 0,
        "registry_drift_flips": 0,
        "candidate_newly_blocking": 0,
        "friction_rate": 0.0,
        "relief_count": 0,
    }


# ── label_proxy_metrics ───────────────────────────────────────────────────────────


def test_label_proxy_metrics_known_confusion_matrix():
    rows = [
        {"ticket_id": "t1", "candidate": ["block"]},  # predicted positive
        {"ticket_id": "t2", "candidate": ["advisory"]},  # predicted negative
        {"ticket_id": "t3", "candidate": ["block"]},  # predicted positive
        {"ticket_id": "t4", "candidate": ["advisory"]},  # predicted negative
    ]
    ticket_labels = {
        "t1": {"escaped_defect": True, "clean_close": False},  # true positive
        "t2": {"escaped_defect": False, "clean_close": True},  # true negative
        "t3": {"escaped_defect": False, "clean_close": True},  # false positive
        "t4": {"escaped_defect": True, "clean_close": False},  # false negative
    }
    metrics = tier0.label_proxy_metrics(rows, ticket_labels)
    assert metrics["coverage_fraction"] == 1.0
    assert metrics["blocking_agreement_rate"] == 0.5  # (tp+tn)/usable = 2/4
    assert metrics["proxy_precision"] == 0.5  # tp/(tp+fp) = 1/2
    assert metrics["proxy_recall"] == 0.5  # tp/(tp+fn) = 1/2


def test_label_proxy_metrics_no_usable_label_gives_none_and_zero_coverage():
    rows = [{"ticket_id": "t1", "candidate": ["block"]}]
    metrics = tier0.label_proxy_metrics(rows, {})
    assert metrics["coverage_fraction"] == 0.0
    assert metrics["blocking_agreement_rate"] is None
    assert metrics["proxy_precision"] is None
    assert metrics["proxy_recall"] is None


def test_ticket_label_from_labels_row_no_usable_label_when_not_closed():
    row = {"escaped_defect": False, "clean_close": False}
    assert tier0.ticket_label_from_labels_row(row) is None


# ── run_tier0 integration (real git tracker) ──────────────────────────────────────


def _review_result_data(
    *,
    findings: list[dict],
    review_phase: str = "planning",
    material_fingerprint: str = "deadbeef00000000",
) -> dict:
    return {
        "schema": "plan_review_result_v2",
        "verdict": "PASS",
        "material_fingerprint": material_fingerprint,
        "findings": findings,
        "review_phase": review_phase,
    }


def test_run_tier0_unknown_candidate_raises(tmp_path):
    with pytest.raises(KeyError):
        tier0.run_tier0(
            {"s": str(tmp_path / "tracker")},
            cache_dir=tmp_path / "cache",
            candidate_name="does-not-exist",
        )


def test_run_tier0_replays_a_real_tracker(tmp_path):
    tracker = TrackerBuilder(tmp_path / "tracker")
    ticket_id = "aaaa-bbbb-cccc-dddd"
    description = "Do the thing."
    tracker.create(ticket_id, description=description)
    finding = _finding(
        criteria=["E2"], block_threshold=0.6, blocking_enabled=True, decision="block"
    )
    tracker.review_result(
        ticket_id,
        data=_review_result_data(
            findings=[finding], material_fingerprint=_fp(ticket_id, description)
        ),
    )

    result = tier0.run_tier0(
        {"s": str(tracker.path)},
        cache_dir=tmp_path / "cache",
        candidate_name="current",
    )
    assert result["row_count"] == 1
    assert result["skipped"] == 0
    assert result["flip_matrix"]["self_check_mismatches"] == 0
    assert result["label_proxy_metrics"] is None


def test_run_tier0_skips_v1_sidecar_lacking_block_threshold(tmp_path):
    tracker = TrackerBuilder(tmp_path / "tracker")
    ticket_id = "aaaa-bbbb-cccc-dddd"
    description = "Do the thing."
    tracker.create(ticket_id, description=description)
    # A v1-shaped finding with no block_threshold at all.
    tracker.review_result(
        ticket_id,
        data=_review_result_data(
            findings=[{"id": "f1", "criteria": ["E1"], "decision": "advisory"}],
            material_fingerprint=_fp(ticket_id, description),
        ),
    )

    result = tier0.run_tier0(
        {"s": str(tracker.path)},
        cache_dir=tmp_path / "cache",
        candidate_name="current",
    )
    assert result["row_count"] == 0
    assert result["skipped"] == 1


# ── legacy_v4_baseline (the plan-v4 relocation) ───────────────────────────────────


def test_legacy_v4_baseline_classify_and_legacy_impact_plan_are_importable():
    from rebar.llm.evals.plan_replay import legacy_v4_baseline

    assert legacy_v4_baseline.classify("undecomposed", "this bundles two independent slices") == (
        "bundles_separable_slices"
    )
    # A finding with no undecomposed/dod severity contributes 0 impact.
    assert legacy_v4_baseline.legacy_impact_plan({}) == 0.0


def test_plan_v5_rescore_script_imports_from_legacy_v4_baseline():
    import importlib.util
    import sys

    repo_root = Path(__file__).resolve().parents[3]
    script_path = repo_root / "docs" / "calibration" / "plan_v5_rescore.py"
    spec = importlib.util.spec_from_file_location("plan_v5_rescore_under_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["plan_v5_rescore_under_test"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("plan_v5_rescore_under_test", None)
    # Every relocated name is present via import, not a local re-definition.
    assert callable(module.classify)
    assert callable(module.legacy_impact_plan)
    assert callable(module.rescore)


def test_render_report_omits_label_proxy_when_absent():
    result = {
        "candidate": "current",
        "content_hash": "abc123",
        "row_count": 1,
        "skipped": 0,
        "flip_matrix": {
            "total_findings": 1,
            "self_check_mismatches": 0,
            "registry_drift_flips": 0,
            "candidate_newly_blocking": 0,
            "friction_rate": 0.0,
            "relief_count": 0,
        },
        "label_proxy_metrics": None,
    }
    text = report.render_report(result)
    assert "not computed" in text
    assert "candidate `current`" in text
