"""RP-03 S5 — coordinator bucket/fuse taxonomy + venue parity (scenarios 1-6).

This suite validates already-merged S1–S4 behaviour: there is NO new production code.
It drives the REAL ``rebar_reconciler.batch_dispatch.coordinate_and_fuse`` pipeline and
covers the coordinator's outcome buckets (applied / recovered / deferred / failed) and
its fuse taxonomy (endpoint / provider / reset).

Design, mirroring the landed unit oracles it extends:

* the coordinator DECISION taxonomy is driven with a scripted, venue-AGNOSTIC ``execute``
  returning ``AtomicSignal``s — exactly the ``test_operation_coordinator.py`` pattern —
  because the coordinator's decision logic performs ZERO I/O, so its outcome is
  venue-neutral BY CONSTRUCTION;
* venue PARITY is pinned by running the SAME applied cutover through TWO real venue
  backends over verified fakes (the Cloud ``AcliClient`` and the DC
  ``JiraDataCenterTransport``), reusing the ``test_coordinator_venue_cutover.py`` harness,
  and asserting byte-identical five-bucket tallies.

The create / restart / partial / delivery scenarios (7-10) live in the sibling module
``test_reconciler_coordinator_taxonomy.py``. The shared, credential-free harness lives in
``_coordinator_harness.py``. Assertions are OBSERVABLE ONLY — report buckets,
``fuse_decision`` fields, recorded wire calls, and the tally dict.
"""

from __future__ import annotations

import pytest
from _coordinator_harness import (
    CLEAN_BUCKETS,
    SUMMARY,
    AtomicSignal,
    FailureScope,
    RecordingAcli,
    RecordingDCClient,
    ScriptedExecutor,
    cloud_backend,
    dc_backend,
    plan,
    run,
    run_coordinator_cutover,
)
from rebar_reconciler.batch_dispatch import build_pass_tally

pytestmark = pytest.mark.interface


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 1 — MIXED SUCCESS (exact five-bucket tally) + venue PARITY
# ════════════════════════════════════════════════════════════════════════════════


def test_s1_mixed_success_exact_five_bucket_tally():
    """A batch where some tickets apply and some fail yields the EXACT five-bucket tally
    and per-ticket buckets; the failure drives the degraded exit."""
    plans = [plan(i) for i in ("J-A1", "J-A2", "O-1")]
    script = {
        "J-A1": AtomicSignal(status="applied"),
        "J-A2": AtomicSignal(status="permanent", scope=FailureScope.ticket),
        "O-1": AtomicSignal(status="applied"),
    }
    report = run(plans, ScriptedExecutor(script))

    assert report.outcome_for("J-A1").bucket == "applied"
    assert report.outcome_for("O-1").bucket == "applied"
    assert report.outcome_for("J-A2").bucket == "failed"
    tally = build_pass_tally(report)
    assert tally["applied_count"] == 2
    assert tally["failed_count"] == 1
    assert tally["deferred_count"] == 0
    assert tally["skipped_count"] == 0
    assert tally["recovered_count"] == 0
    assert tally["buckets"] == {
        "applied": 2,
        "recovered": 0,
        "deferred": 0,
        "failed": 1,
        "skipped": 0,
    }
    assert tally["degraded"] is True
    # A ticket-scoped failure never masks the independent applied tickets as deferred.
    assert report.outcome_for("J-A2").fuse_decision is None


def test_s1_venue_parity_cloud_and_dc_applied_cutover(tmp_path, monkeypatch):
    """venue PARITY: the SAME applied summary cutover through the real Cloud and DC
    backends reaches each venue's own wire as EXACTLY ONE physical mutation and yields
    byte-identical five-bucket tallies — the coordinator is venue-neutral."""
    rec = RecordingAcli()
    cloud = cloud_backend(monkeypatch, rec)
    cloud_report, cloud_tally, cloud_sink = run_coordinator_cutover(
        cloud.transport, tmp_path, "DIG-1"
    )

    dc_client = RecordingDCClient()
    dc = dc_backend(dc_client)
    dc_report, dc_tally, dc_sink = run_coordinator_cutover(dc.transport, tmp_path, "DC-1")

    assert cloud.vendor == "jira"
    assert dc.vendor == "jira-datacenter"
    # Exactly one physical mutation per venue (no dual-send), summary verbatim.
    (cloud_cmd,) = rec.workitem_edits
    assert cloud_cmd[cloud_cmd.index("--summary") + 1] == SUMMARY
    assert dc_client.field_edits == [{"summary": SUMMARY}]
    # PARITY: byte-identical clean tallies.
    assert cloud_tally["buckets"] == dc_tally["buckets"] == CLEAN_BUCKETS
    assert cloud_tally["degraded"] is dc_tally["degraded"] is False
    assert (
        cloud_report.outcome_for("DIG-1").bucket
        == dc_report.outcome_for("DC-1").bucket
        == "applied"
    )
    assert not cloud_sink[0].get("error")
    assert not dc_sink[0].get("error")


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 2 — TRANSIENT RECOVERY (recovered bucket; folds into applied_count)
# ════════════════════════════════════════════════════════════════════════════════


def test_s2_transient_recovery_folds_into_applied_yet_reports_recovered():
    """An operation that heals within budget lands in the ``recovered`` bucket;
    ``build_pass_tally`` folds it into ``applied_count`` while ``recovered_count`` stays
    exact and NONZERO."""
    plans = [plan("J-A1"), plan("O-1")]
    script = {"J-A1": AtomicSignal(status="recovered")}
    report = run(plans, ScriptedExecutor(script))

    assert report.outcome_for("J-A1").bucket == "recovered"
    assert report.outcome_for("O-1").bucket == "applied"
    tally = build_pass_tally(report)
    assert tally["recovered_count"] == 1
    assert tally["applied_count"] == 2  # the recovered success folds in
    assert tally["failed_count"] == 0
    assert tally["buckets"]["recovered"] == 1
    assert tally["buckets"]["applied"] == 1


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 3 — LONG-DELAY DEFERRAL (endpoint fuse; concrete retry_not_before)
# ════════════════════════════════════════════════════════════════════════════════


def test_s3_long_delay_deferral_endpoint_fuse_with_retry_not_before():
    """Enough same-endpoint exhaustion opens the endpoint fuse; a later matching ticket
    is ``deferred`` carrying the exact fuse scope / reason / deterministic
    ``retry_not_before`` (now_ms=0, cooldown_ms=60000 → 1970-01-01T00:01:00Z), while an
    independent github ticket applies."""
    plans = [plan(i) for i in ("T-1", "T-2", "T-3", "T-4")]
    plans.append(plan("O-1"))

    def execute(ticket_plan, mutation):
        if ticket_plan.identity == "O-1":
            return AtomicSignal(status="applied")
        return AtomicSignal(status="transient")

    report = run(plans, execute, cooldown_ms=60000)

    assert report.outcome_for("O-1").bucket == "applied"
    for ident in ("T-1", "T-2", "T-3"):
        assert report.outcome_for(ident).bucket == "failed"
    t4 = report.outcome_for("T-4")
    assert t4.bucket == "deferred"
    assert t4.fuse_decision is not None
    assert t4.fuse_decision.scope == "endpoint"
    assert t4.fuse_decision.reason
    assert t4.fuse_decision.retry_not_before == "1970-01-01T00:01:00Z"
    assert len(report.fuse_decisions) == 1
    assert report.fuse_decisions[0].scope == "endpoint"
    assert report.degraded is True
    # The deferred outcome's failure_scope is the SAME FailureScope enum member.
    assert t4.failure_scope == FailureScope.endpoint


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 4 — PERMANENT TICKET FAILURE (never masked, even under an open scope)
# ════════════════════════════════════════════════════════════════════════════════


def test_s4_permanent_failure_not_masked_under_open_scope():
    """A genuine ``permanent`` signal is ``failed`` and drives ``degraded=True``; under an
    already-open endpoint scope it is NEVER reclassified to ``deferred`` — an open fuse
    must not mask a real failure."""
    plans = [plan(i) for i in ("T-1", "T-2", "T-3", "T-4")]

    def execute(ticket_plan, mutation):
        if ticket_plan.identity == "T-4":
            return AtomicSignal(status="permanent")
        return AtomicSignal(status="transient")

    report = run(plans, execute)

    t4 = report.outcome_for("T-4")
    assert t4.bucket == "failed"
    assert t4.fuse_decision is None  # not masked as deferred
    assert report.degraded is True


def test_s4_simple_permanent_failure_is_failed_and_degraded():
    """A lone ``permanent`` signal (no open scope) is ``failed`` and degraded — the
    minimal permanent-failure bucket."""
    report = run([plan("J-A1")], ScriptedExecutor({"J-A1": AtomicSignal(status="permanent")}))
    assert report.outcome_for("J-A1").bucket == "failed"
    assert build_pass_tally(report)["failed_count"] == 1
    assert report.degraded is True


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 5 — AUTH/PROVIDER STOP (provider-scope fuse isolates to its provider)
# ════════════════════════════════════════════════════════════════════════════════


def test_s5_provider_scope_fuse_isolates_and_never_conflates_independent_provider():
    """Fuse-eligible exhaustion spanning two endpoints of ONE provider opens a
    PROVIDER-scope fuse; remaining matching-provider work is ``deferred`` carrying
    ``fuse_decision.scope == 'provider'``, while an INDEPENDENT provider's ticket stays
    ``applied`` — scopes are isolated, never conflated."""
    order = ("J-A1", "J-A2", "J-B1", "J-B2", "J-C1", "G-1")
    plans = [plan(i) for i in order]

    def execute(ticket_plan, mutation):
        if ticket_plan.identity == "G-1":
            return AtomicSignal(status="applied")
        return AtomicSignal(status="transient")

    report = run(plans, execute, cooldown_ms=60000)

    # The independent github provider is never touched by the jira provider fuse.
    assert report.outcome_for("G-1").bucket == "applied"
    assert report.outcome_for("G-1").fuse_decision is None
    # A provider-scope fuse opened (spanning >=2 jira endpoints).
    assert [d.scope for d in report.fuse_decisions] == ["provider"]
    # Remaining matching-provider work is deferred under the provider scope.
    deferred = [i for i in order if report.outcome_for(i).bucket == "deferred"]
    assert deferred, "expected provider-scoped work to be deferred once the fuse opened"
    for ident in deferred:
        fd = report.outcome_for(ident).fuse_decision
        assert fd is not None
        assert fd.scope == "provider"
        assert report.outcome_for(ident).failure_scope == FailureScope.provider
    assert report.degraded is True


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 6 — FUSE OPEN THEN RESET (same-scope success re-closes; reported applied)
# ════════════════════════════════════════════════════════════════════════════════


def test_s6_success_after_fuse_open_recloses_scope_and_is_applied():
    """A same-endpoint SUCCESS arriving AFTER the fuse opened re-closes the scope and is
    reported ``applied`` (never deferred) — proven health beats an open fuse."""
    plans = [plan(i) for i in ("T-1", "T-2", "T-3", "T-4")]

    def execute(ticket_plan, mutation):
        if ticket_plan.identity == "T-4":
            return AtomicSignal(status="applied")
        return AtomicSignal(status="transient")

    report = run(plans, execute)

    t4 = report.outcome_for("T-4")
    assert t4.bucket == "applied"
    assert t4.fuse_decision is None
