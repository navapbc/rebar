"""AC2 (rebar-ticket 67d4-ecba-cf95-4353) — venue-gated Cloud/DC verified-fake
coordinator-cutover tests.

RP-03 S3 T3 wired the non-create mutation families onto the S3 coordinator+fuse
route (``route_for`` defaults ``update`` to ``coordinator``, and
``coordinate_and_fuse`` / ``make_guarded_execute`` / ``make_coordinator_dispatch`` /
``map_cutover_report`` in ``rebar_reconciler.batch_dispatch`` drive it). The T3
oracle in ``test_operation_coordinator.py`` proved that pipeline against a
venue-AGNOSTIC in-memory ``execute`` fake — it has NO Cloud-vs-DC dimension.

This module is the venue-gated half AC2 asks for: **"Venue-gated Cloud/DC verified
fakes pass before the coordinator default changes."** It drives the SAME landed
coordinator cutover — the real ``coordinate_and_fuse`` → ``make_guarded_execute`` →
``make_coordinator_dispatch`` → ``apply_handlers.dispatch_mutation`` → ``update_one`` →
``client.update_issue`` chain — for a non-create SUMMARY UPDATE (the canonical case
named in the plan) through TWO real venue backends over verified fakes:

* a **Cloud** backend (``AcliClient``) whose ACLI subprocess seam
  (``acli_subprocess._run_acli``) is stubbed, so the assertion is on the physical
  ``jira workitem edit`` argv the Cloud wire actually received; and
* a **DC** backend (``JiraDataCenterTransport`` under ``JiraDataCenterBackend``) whose
  ``jira.JIRA`` client is stubbed, so the assertion is on the ``issue.update(fields=…)``
  REST field-edit the DC wire actually received.

**Interpretation (venue PARITY, not a manufactured venue DIFFERENCE).** A summary is a
scalar string field on BOTH venues; the per-venue rich-text serialization seam
(Cloud ADF vs DC wiki) lives on the *description* path, not summary — see
``adapters/jira/acli.py:update_issue`` (``if field == "description": … _text_to_adf``,
everything else ``str(value)``) and ``adapters/jira_datacenter/_issues.py:update_issue``
(a plain ``issue.update(fields=kwargs)``). The coordinator cutover therefore delegates
summary serialization to a lower adapter layer that is venue-agnostic FOR SUMMARY. So
per the task's judgment call, the correct venue-gated verified fake runs the same
cutover batch against a Cloud-configured and a DC-configured dispatch and pins PARITY:
each venue receives EXACTLY ONE physical mutation (no dual-send), carrying the summary
verbatim to that venue's own wire shape, with identical five-bucket
(applied/failed/deferred/skipped/recovered) tallies from the ``CutoverReport``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from rebar_reconciler.adapters.jira import acli
from rebar_reconciler.adapters.jira.backend import JiraBackend
from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend
from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport
from rebar_reconciler.apply_handlers import BatchApplyContext
from rebar_reconciler.batch_dispatch import (
    coordinate_and_fuse,
    make_coordinator_dispatch,
    make_guarded_execute,
    map_cutover_report,
    route_for,
)
from rebar_reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar_reconciler.retry_budget import RetryBudget
from rebar_reconciler.ticket_plan import PlanDisposition, TicketPlan

pytestmark = pytest.mark.unit

#: The summary these cutovers push to each venue's wire. A plain scalar string — the
#: value the physical call must carry verbatim (never ADF/wiki-wrapped on either venue).
_SUMMARY = "AC2 venue-gated coordinator summary"

#: The clean five-bucket tally a single applied summary update must produce on BOTH
#: venues. Declared once so the parity test can assert Cloud == DC == this.
_CLEAN_BUCKETS = {"applied": 1, "recovered": 0, "deferred": 0, "failed": 0, "skipped": 0}


class _FrozenClock:
    """A frozen clock for the coordinator's ``RetryBudget`` — no wall-clock I/O.

    ``now`` never advances and ``sleep_ms`` is recorded, not slept, so the cutover is
    deterministic (parity with ``test_operation_coordinator.py``'s ``_Clock``)."""

    def __init__(self) -> None:
        self.slept: list[int] = []

    def now(self) -> int:
        return 0

    def sleep_ms(self, ms: int) -> None:
        self.slept.append(ms)


def _budget_factory():
    def factory() -> RetryBudget:
        return RetryBudget(clock=_FrozenClock(), jitter=lambda: 0.0)

    return factory


def _locate(identity: str) -> dict[str, Any]:
    # One jira ticket on one endpoint — enough for the fuse to key a scope on, and
    # identical for both venues so any tally divergence is a real venue difference.
    return {identity: {"provider": "jira", "endpoint": "https://venue.example"}}.get(identity, {})


def _summary_update_plan(key: str) -> TicketPlan:
    """A single-ticket plan carrying ONE non-create summary UPDATE mutation."""
    mutation = Mutation(
        direction=MutationDirection.outbound,
        action=MutationAction.update,
        target=key,
        payload={"changed_fields": {"summary": _SUMMARY}},
        provenance={"src": "ac2-venue-cutover"},
    )
    return TicketPlan(
        identity=key,
        mutations=(mutation,),
        diagnostics=(),
        disposition=PlanDisposition("mutate"),
        observation_version="ov-ac2",
        payload={},
        dependencies=(),
        defer_reason=None,
    )


def _run_coordinator_cutover(client: Any, repo_root, key: str):
    """Drive the LANDED S3 T3 coordinator cutover for a summary UPDATE over ``client``.

    This is the production wiring, not a re-implementation: the guarded execute runs
    the same abort/drift preamble, and ``make_coordinator_dispatch`` is built with the
    default ``dispatch=None`` so the physical attempt flows through the real
    ``apply_handlers.dispatch_mutation`` → ``update_one`` → ``client.update_issue``.
    ``client`` is the venue transport that ``BatchApplyContext.client`` binds in
    production (``applier`` resolves ``select_backend(...).transport``)."""
    # AC1 precondition: the non-create default IS the coordinator route (cutover on).
    assert route_for("update") == "coordinator"
    ctx = BatchApplyContext(client=client, repo_root=repo_root, pass_id="ac2-pass")
    outcomes_sink: list[dict] = []
    dispatch_fn = make_coordinator_dispatch(ctx=ctx, outcomes_sink=outcomes_sink)
    head_pin_cell = ["pin0"]
    execute = make_guarded_execute(
        abort_check=None,
        recheck_drift=lambda _concurrency, _repo_root, pin: pin,
        concurrency=None,
        repo_root=repo_root,
        head_pin_cell=head_pin_cell,
        dispatch_fn=dispatch_fn,
    )
    report = coordinate_and_fuse(
        [_summary_update_plan(key)],
        execute=execute,
        locate=_locate,
        budget_factory=_budget_factory(),
        now_ms=0,
    )
    return report, map_cutover_report(report), outcomes_sink


# ── Cloud verified fake ──────────────────────────────────────────────────────────


class _RecordingAcli:
    """Records the ACLI argv the Cloud wire receives, stubbing the subprocess seam.

    ``AcliClient.update_issue`` builds a ``jira workitem edit`` command and hands it to
    ``acli_subprocess._run_acli``; this stand-in captures every argv and returns the
    ``{"key": …}`` stdout the client parses, so nothing spawns a real subprocess."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def run_acli(self, cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        self.argvs.append(list(cmd))
        key = cmd[cmd.index("--key") + 1] if "--key" in cmd else "DIG-1"
        return SimpleNamespace(stdout=json.dumps({"key": key}))

    @property
    def workitem_edits(self) -> list[list[str]]:
        return [cmd for cmd in self.argvs if cmd[:3] == ["jira", "workitem", "edit"]]


def test_cloud_venue_gated_coordinator_summary_cutover(tmp_path, monkeypatch):
    """venue-gated Cloud verified fake (AC2): a non-create summary UPDATE driven
    through the landed coordinator cutover reaches the Cloud (``AcliClient``) wire as
    EXACTLY ONE ``jira workitem edit`` carrying the summary verbatim — one physical
    mutation, no dual-send — and the ``CutoverReport`` tallies it as a single applied
    op with no failures/defers/skips."""
    rec = _RecordingAcli()
    monkeypatch.setattr(acli.acli_subprocess, "_run_acli", rec.run_acli)
    client = acli.AcliClient(
        jira_url="https://example.atlassian.net",
        user="bot@example.com",
        api_token="t",
        jira_project="DIG",
    )
    # The Cloud backend is the real venue object (identity gate) built over this wire.
    backend = JiraBackend(transport=client)
    assert backend.vendor == "jira"

    report, tally, sink = _run_coordinator_cutover(backend.transport, tmp_path, "DIG-1")

    # Verified-fake wire: EXACTLY ONE physical mutation on the Cloud wire (no dual-send).
    edits = rec.workitem_edits
    assert len(edits) == 1, f"expected one Cloud workitem edit, got {rec.argvs!r}"
    (cmd,) = edits
    assert cmd[cmd.index("--key") + 1] == "DIG-1"
    # Cloud body serialization for a scalar summary: the raw string, NOT ADF-wrapped
    # (ADF is the description-only seam — a summary that arrived as an ADF doc would be
    # a serialization regression).
    assert "--summary" in cmd, f"summary field missing from Cloud edit: {cmd!r}"
    wire_summary = cmd[cmd.index("--summary") + 1]
    assert wire_summary == _SUMMARY
    assert not wire_summary.startswith("{"), "summary must reach Cloud as plain text, not ADF"

    # Five-bucket tally from the CutoverReport: one applied, nothing else, not degraded.
    assert tally["applied_count"] == 1
    assert tally["failed_count"] == 0
    assert tally["deferred_count"] == 0
    assert tally["skipped_count"] == 0
    assert tally["recovered_count"] == 0
    assert tally["degraded"] is False
    assert tally["buckets"] == _CLEAN_BUCKETS
    assert report.outcome_for("DIG-1").bucket == "applied"
    # One outcome recorded, error-free.
    assert len(sink) == 1 and not sink[0].get("error")


# ── DC verified fake ─────────────────────────────────────────────────────────────


class _RecordingDCIssue:
    """A ``jira.Issue``-shaped stand-in: records the field edit the DC wire receives."""

    def __init__(self, key: str, field_edits: list[dict]) -> None:
        self.key = key
        self.raw = {"key": key, "fields": {}}
        self._field_edits = field_edits

    def update(self, fields: dict | None = None, **_kwargs: Any) -> None:
        self._field_edits.append(dict(fields or {}))
        self.raw["fields"].update(fields or {})


class _RecordingDCClient:
    """A ``jira.JIRA``-shaped stand-in for the DC transport's underlying client.

    ``JiraDataCenterTransport.update_issue`` fetches the issue and calls
    ``issue.update(fields=…)`` (the DC REST field-edit), then reads it back via
    ``get_issue``; this records every field-edit body so the wire is observable."""

    def __init__(self) -> None:
        self.field_edits: list[dict] = []
        self.issued: list[str] = []

    def issue(self, key: str) -> _RecordingDCIssue:
        self.issued.append(key)
        return _RecordingDCIssue(key, self.field_edits)


def test_dc_venue_gated_coordinator_summary_cutover(tmp_path):
    """venue-gated DC verified fake (AC2): the SAME non-create summary UPDATE driven
    through the landed coordinator cutover reaches the Data Center
    (``JiraDataCenterTransport``) wire as EXACTLY ONE ``issue.update(fields=…)`` REST
    field-edit carrying ``{"summary": …}`` verbatim — one physical mutation, no
    dual-send — and the ``CutoverReport`` tallies it as a single applied op."""
    dc_client = _RecordingDCClient()
    transport = JiraDataCenterTransport(client=dc_client, project="FAKE")
    backend = JiraDataCenterBackend(transport, client=dc_client)
    assert backend.vendor == "jira-datacenter"

    report, tally, sink = _run_coordinator_cutover(backend.transport, tmp_path, "DC-1")

    # Verified-fake wire: EXACTLY ONE physical field-edit on the DC wire (no dual-send).
    assert len(dc_client.field_edits) == 1, (
        f"expected one DC field edit, got {dc_client.field_edits!r}"
    )
    (fields,) = dc_client.field_edits
    # DC body serialization for a scalar summary: a plain ``{"summary": <str>}`` field
    # edit (wiki rendering is the description-only seam; summary is passed through).
    assert fields == {"summary": _SUMMARY}

    # Five-bucket tally from the CutoverReport: one applied, nothing else, not degraded.
    assert tally["applied_count"] == 1
    assert tally["failed_count"] == 0
    assert tally["deferred_count"] == 0
    assert tally["skipped_count"] == 0
    assert tally["recovered_count"] == 0
    assert tally["degraded"] is False
    assert tally["buckets"] == _CLEAN_BUCKETS
    assert report.outcome_for("DC-1").bucket == "applied"
    assert len(sink) == 1 and not sink[0].get("error")


# ── Cloud/DC parity ──────────────────────────────────────────────────────────────


def test_cloud_dc_venue_parity_through_coordinator_cutover(tmp_path, monkeypatch):
    """venue-gated Cloud/DC PARITY (AC2): the landed coordinator cutover routes the
    same non-create summary UPDATE through BOTH venues with identical observable
    behavior — one physical mutation each (no dual-send), the summary on each venue's
    own wire, and byte-identical five-bucket tallies. This is the parity form of the
    verified fake: the cutover is venue-gated (each side is a real venue backend) yet
    venue-agnostic FOR SUMMARY by design, so Cloud and DC must not diverge."""
    # Cloud leg.
    rec = _RecordingAcli()
    monkeypatch.setattr(acli.acli_subprocess, "_run_acli", rec.run_acli)
    cloud = acli.AcliClient(
        jira_url="https://example.atlassian.net",
        user="bot@example.com",
        api_token="t",
        jira_project="DIG",
    )
    cloud_backend = JiraBackend(transport=cloud)
    cloud_report, cloud_tally, cloud_sink = _run_coordinator_cutover(
        cloud_backend.transport, tmp_path, "DIG-1"
    )

    # DC leg.
    dc_client = _RecordingDCClient()
    dc_backend = JiraDataCenterBackend(
        JiraDataCenterTransport(client=dc_client, project="FAKE"), client=dc_client
    )
    dc_report, dc_tally, dc_sink = _run_coordinator_cutover(dc_backend.transport, tmp_path, "DC-1")

    # Distinct venues actually exercised.
    assert cloud_backend.vendor == "jira"
    assert dc_backend.vendor == "jira-datacenter"

    # One physical mutation per venue — no dual-send on either wire.
    assert len(rec.workitem_edits) == 1
    assert len(dc_client.field_edits) == 1

    # The summary reached each venue's own wire shape.
    (cloud_cmd,) = rec.workitem_edits
    assert cloud_cmd[cloud_cmd.index("--summary") + 1] == _SUMMARY
    assert dc_client.field_edits[0] == {"summary": _SUMMARY}

    # PARITY: byte-identical five-bucket tallies and clean, non-degraded passes.
    assert cloud_tally["buckets"] == dc_tally["buckets"] == _CLEAN_BUCKETS
    assert cloud_tally["applied_count"] == dc_tally["applied_count"] == 1
    assert cloud_tally["degraded"] is dc_tally["degraded"] is False
    assert cloud_report.outcome_for("DIG-1").bucket == dc_report.outcome_for("DC-1").bucket
    assert not cloud_sink[0].get("error")
    assert not dc_sink[0].get("error")
