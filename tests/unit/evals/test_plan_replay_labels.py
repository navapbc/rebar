"""Tests for outcome labels + the reviewer-noise floor (``rebar.llm.evals.plan_replay.labels``,
ticket expectable-clownlike-ynambu).

Pure classification/math functions are tested directly with plain dicts (fast, precise --
this is where every plan-review round's contention lived: escape-signal composition, the
FAIL/PASS schema discriminator sharing one event TYPE, ns-timestamp conversion, finding
survival classification, multi-membership per-criterion churn, nested per-question
agreement). ``build_labels``/``load_labels`` integration is exercised against a REAL git
tracker (mirroring test_plan_replay_corpus.py's TrackerBuilder), since that is where the
git-object-walk and content-hash machinery actually runs.
"""

from __future__ import annotations

import json
import subprocess
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rebar.llm.evals.plan_replay import labels
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import material_fingerprint

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


def _event(kind: str, data: dict, ts: int | None = None) -> dict:
    return {"kind": kind, "ts": ts if ts is not None else _next_ts(), "data": data}


def _finding(norm_id: str, criteria: list[str], verification: dict | None = None) -> dict:
    d = {"norm_id": norm_id, "criteria": criteria}
    if verification is not None:
        d["verification"] = verification
    return d


# ── happy path ──────────────────────────────────────────────────────────────────
def test_escaped_defect_true_via_close_class():
    assert labels.escaped_defect({"close_class": "plan_defect", "inbound_deps": []}) is True


def test_escape_signals_false_when_nothing_fires():
    assert (
        labels.escape_signals(escaped=False, completion_failed=False, reopened=False, forced=False)
        is False
    )


def test_classify_finding_survival_persisted_and_resolved():
    review_k = [_finding("n1", ["G6"]), _finding("n2", ["E2"])]
    review_k1 = [_finding("n1", ["G6"])]  # n2 removed, n1 stays

    result = labels.classify_finding_survival(review_k, review_k1)

    assert result == {"n1": "persisted", "n2": "resolved_by_author"}


def test_per_criterion_churn_identical_material_is_zero():
    findings = [_finding("n1", ["G6"])]
    result = labels.per_criterion_churn(findings, findings)
    assert result["per_criterion"]["G6"] == pytest.approx(0.0)
    assert result["mean"] == pytest.approx(0.0)


# ── edge: escape_signals composition (the "clean_close undefined escape set" finding) ──
@pytest.mark.parametrize(
    "kwargs",
    [
        {"escaped": True, "completion_failed": False, "reopened": False, "forced": False},
        {"escaped": False, "completion_failed": True, "reopened": False, "forced": False},
        {"escaped": False, "completion_failed": False, "reopened": True, "forced": False},
        {"escaped": False, "completion_failed": False, "reopened": False, "forced": True},
    ],
)
def test_escape_signals_true_when_any_one_fires(kwargs):
    assert labels.escape_signals(**kwargs) is True


# ── edge: escaped_defect direction (caused_by is inbound-only on the culprit) ──────
def test_escaped_defect_true_via_inbound_caused_by():
    state = {"close_class": None, "inbound_deps": [{"relation": "caused_by", "from_id": "bug1"}]}
    assert labels.escaped_defect(state) is True


def test_escaped_defect_false_when_caused_by_is_outbound_only():
    # An outbound caused_by (this ticket IS the bug pointing at another) must not
    # itself satisfy the inbound disjunct.
    state = {"close_class": None, "inbound_deps": [{"relation": "depends_on", "from_id": "x"}]}
    assert labels.escaped_defect(state) is False


# ── edge: completion_verifier_fail_count / completion_failed_after_pass discriminator ──
def test_completion_verifier_fail_count_ignores_pass_schema():
    events = [
        _event("COMPLETION_VERDICT", {"schema": "completion_verifier_pass_v1"}),
        _event("COMPLETION_VERDICT", {"schema": "completion_verifier_fail_v1"}),
    ]
    assert labels.completion_verifier_fail_count(events) == 1


def test_completion_failed_after_pass_true_only_for_fail_after_ts():
    pass_ts = 1_000_000_000_000_000_000
    events = [
        _event("COMPLETION_VERDICT", {"schema": "completion_verifier_fail_v1"}, ts=pass_ts + 1),
    ]
    assert labels.completion_failed_after_pass(pass_ts, events) is True


def test_completion_failed_after_pass_false_when_fail_precedes_pass():
    pass_ts = 1_000_000_000_000_000_000
    events = [
        _event("COMPLETION_VERDICT", {"schema": "completion_verifier_fail_v1"}, ts=pass_ts - 1),
    ]
    assert labels.completion_failed_after_pass(pass_ts, events) is False


def test_completion_failed_after_pass_false_for_pass_schema_after_ts():
    pass_ts = 1_000_000_000_000_000_000
    events = [
        _event("COMPLETION_VERDICT", {"schema": "completion_verifier_pass_v1"}, ts=pass_ts + 1),
    ]
    assert labels.completion_failed_after_pass(pass_ts, events) is False


# ── edge: reopen_count / force_close ────────────────────────────────────────────
def test_reopen_count_counts_closed_to_open_transitions():
    events = [
        _event("STATUS", {"current_status": "closed", "status": "open"}),
        _event("STATUS", {"current_status": "open", "status": "in_progress"}),
    ]
    assert labels.reopen_count(events) == 1


def test_force_close_detects_force_close_comment_prefix():
    events = [_event("COMMENT", {"body": "FORCE_CLOSE: overriding gate"})]
    assert labels.force_close(events) is True


def test_force_close_false_for_ordinary_comment():
    events = [_event("COMMENT", {"body": "looks good"})]
    assert labels.force_close(events) is False


# ── edge: clean_close ns-timestamp conversion + escape gating ──────────────────
def test_clean_close_true_when_old_enough_and_no_escape():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    ten_days_ago = datetime(2026, 1, 5, tzinfo=timezone.utc)
    close_ts_ns = int(ten_days_ago.timestamp() * 1e9)

    assert (
        labels.clean_close(closed=True, escape=False, latest_close_ts_ns=close_ts_ns, now=now)
        is True
    )


def test_clean_close_false_when_too_recent():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    one_day_ago = datetime(2026, 1, 14, tzinfo=timezone.utc)
    close_ts_ns = int(one_day_ago.timestamp() * 1e9)

    assert (
        labels.clean_close(closed=True, escape=False, latest_close_ts_ns=close_ts_ns, now=now)
        is False
    )


def test_clean_close_false_when_escape_signal_fires_even_if_old():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    close_ts_ns = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1e9)

    assert (
        labels.clean_close(closed=True, escape=True, latest_close_ts_ns=close_ts_ns, now=now)
        is False
    )


def test_clean_close_false_when_not_closed():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert labels.clean_close(closed=False, escape=False, latest_close_ts_ns=1, now=now) is False


def test_latest_close_ts_picks_the_most_recent_close_transition():
    events = [
        _event("STATUS", {"status": "closed"}, ts=100),
        _event("STATUS", {"status": "open"}, ts=200),
        _event("STATUS", {"status": "closed"}, ts=300),
    ]
    assert labels.latest_close_ts(events) == 300


# ── edge: finding survival — new findings get neither label ────────────────────
def test_classify_finding_survival_new_finding_gets_no_label():
    review_k = [_finding("n1", ["G6"])]
    review_k1 = [_finding("n1", ["G6"]), _finding("n2", ["E2"])]

    result = labels.classify_finding_survival(review_k, review_k1)

    assert result == {"n1": "persisted"}
    assert "n2" not in result


# ── edge: per-criterion churn multi-membership ──────────────────────────────────
def test_per_criterion_churn_multi_membership_finding_contributes_to_both():
    review_k = [_finding("n1", ["E2", "T8"])]
    review_k1: list[dict] = []  # n1 removed entirely

    result = labels.per_criterion_churn(review_k, review_k1)

    assert set(result["per_criterion"]) == {"E2", "T8"}
    assert result["per_criterion"]["E2"] == pytest.approx(1.0)
    assert result["per_criterion"]["T8"] == pytest.approx(1.0)


# ── edge: per-question agreement over nested verification ──────────────────────
def test_per_question_agreement_over_nested_verification_one_match_one_mismatch():
    review_k = [
        _finding(
            "n1",
            ["G6"],
            verification={"binary": {"a": True, "b": True}, "severity_attributes": {}},
        )
    ]
    review_k1 = [
        _finding(
            "n1",
            ["G6"],
            verification={"binary": {"a": True, "b": False}, "severity_attributes": {}},
        )
    ]

    result = labels.per_question_agreement(review_k, review_k1)

    assert result["comparisons"] == 2
    assert result["agreement"] == pytest.approx(0.5)


def test_per_question_agreement_none_when_no_verification_present():
    review_k = [_finding("n1", ["G6"])]
    review_k1 = [_finding("n1", ["G6"])]

    result = labels.per_question_agreement(review_k, review_k1)

    assert result["comparisons"] == 0
    assert result["agreement"] is None


def test_per_question_agreement_tolerates_explicit_null_verification():
    """A finding can carry the key "verification" present with an explicit ``None``
    value (a real shape seen in production data) -- .get("verification") returning
    None must not crash, only skip that finding for the comparison."""
    review_k = [{"norm_id": "n1", "criteria": ["G6"], "verification": None}]
    review_k1 = [{"norm_id": "n1", "criteria": ["G6"], "verification": None}]

    result = labels.per_question_agreement(review_k, review_k1)

    assert result["comparisons"] == 0
    assert result["agreement"] is None


# ── edge: noise_flip only for identical material ────────────────────────────────
def test_noise_flip_true_on_differing_verdict():
    assert labels.noise_flip({"verdict": "PASS"}, {"verdict": "BLOCK"}) is True


def test_noise_flip_false_on_same_verdict():
    assert labels.noise_flip({"verdict": "PASS"}, {"verdict": "PASS"}) is False


# ── E2E: build_labels + load_labels against a real git tracker ──────────────────
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


def _stub_ticket_state(**overrides):
    state = {"status": "open", "close_class": None, "inbound_deps": []}
    state.update(overrides)
    return lambda ticket_id, tracker_path: state


def test_build_labels_writes_a_hash_keyed_file_and_load_labels_round_trips(tmp_path):
    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0001"
    tracker.create(ticket_id, description="Plan text.")
    tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": _fp(ticket_id, "Plan text."),
            "reviewed_related_material": [],
            "findings": [],
        },
    )

    out_dir = tmp_path / "labels_out"
    manifest = labels.build_labels(
        {"main": str(tracker.path)},
        cache_dir=tmp_path / "cache",
        out_dir=out_dir,
        read_ticket_state=_stub_ticket_state(),
    )

    out_path = out_dir / f"labels-{manifest['content_hash']}.jsonl"
    assert out_path.exists()
    report_path = out_dir / f"labels-report-{manifest['content_hash']}.md"
    assert report_path.exists()
    assert "Plan-review outcome labels" in report_path.read_text()

    loaded = labels.load_labels(
        str(out_path), store_roots={"main": str(tracker.path)}, cache_dir=tmp_path / "cache"
    )
    assert isinstance(loaded, list)
    assert loaded[0]["ticket_labels"] == {
        "escaped_defect": False,
        "completion_failed_after_pass": False,
        "reopened": False,
        "reopen_count": 0,
        "force_close": False,
        "completion_verifier_fail_count": 0,
        "clean_close": False,
    }


def test_build_labels_ticket_labels_reflect_escaped_defect_via_stub(tmp_path):
    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0003"
    tracker.create(ticket_id, description="Plan text.")
    tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": _fp(ticket_id, "Plan text."),
            "reviewed_related_material": [],
            "findings": [],
        },
    )

    out_dir = tmp_path / "labels_out"
    manifest = labels.build_labels(
        {"main": str(tracker.path)},
        cache_dir=tmp_path / "cache",
        out_dir=out_dir,
        read_ticket_state=_stub_ticket_state(close_class="plan_defect"),
    )

    loaded = labels.load_labels(
        str(out_dir / f"labels-{manifest['content_hash']}.jsonl"),
        store_roots={"main": str(tracker.path)},
        cache_dir=tmp_path / "cache",
    )
    assert loaded[0]["ticket_labels"]["escaped_defect"] is True

    report = (out_dir / f"labels-report-{manifest['content_hash']}.md").read_text()
    assert "escaped_defect: 1" in report


def test_load_labels_refuses_a_stale_hash(tmp_path):
    tracker = TrackerBuilder(tmp_path / "store")
    tracker.create("0000-0000-0000-0002", description="Plan text.")

    stale_path = tmp_path / "labels_out" / "labels-deadbeefdeadbeef.jsonl"
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_text("")

    with pytest.raises(labels.LabelsHashMismatch):
        labels.load_labels(
            str(stale_path), store_roots={"main": str(tracker.path)}, cache_dir=tmp_path / "cache"
        )
