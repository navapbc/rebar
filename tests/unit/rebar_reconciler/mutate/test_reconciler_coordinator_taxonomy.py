"""RP-03 S5 — create / restart / partial / delivery taxonomy (scenarios 7-10).

The sibling module ``test_reconciler_coordinator.py`` covers the coordinator's bucket and
fuse taxonomy through ``coordinate_and_fuse`` directly. This module covers the scenarios
whose real home is a layer AROUND the coordinator, driving each real entry point against a
fake backend:

* scenario 7 — the create route (``apply_handlers.dispatch_mutation``), asserting the
  issue is created BEFORE labels are added and NO ``delete_issue`` is ever called;
* scenario 8 — the transition-replay resume (``transition_replay.replay_transition``) plus
  the observe-before-replay guard (``retry_budget.decide_replay`` /
  ``operation_outcome.replay_safety_for``);
* scenario 9 — the ``commit_unknown`` partial outcome (its own non-applied bucket,
  replay-FORBIDDEN);
* scenario 10 — the fail-open delivery invariant (``pass_io.record_parent_divergence``):
  the observability sink failing must never cost a mutation that would land.

The shared, credential-free harness lives in ``_coordinator_harness.py``. Everything is
credential-free: no network, no real Jira, a frozen clock, no wall-clock sleep.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar_reconciler import pass_io
from rebar_reconciler.apply_handlers import BatchApplyContext, dispatch_mutation
from rebar_reconciler.binding_store import BindingStore
from rebar_reconciler.retry_budget import decide_replay, replay_safety_for
from rebar_reconciler.transition_replay import replay_transition

from ._coordinator_harness import (
    CLEAN_BUCKETS,
    Disposition,
    RecordingAcli,
    ReplaySafety,
    cloud_backend,
    run_coordinator_cutover,
)

pytestmark = pytest.mark.unit


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 7 — LABEL-FIRST CREATE (create BEFORE label; NEVER a remote delete)
# ════════════════════════════════════════════════════════════════════════════════


class _RecordingCreateClient:
    """A declared-protocol transport fake recording every physical call, in ORDER.

    Provider-neutral (stands in for either Cloud or DC). ``delete_issue`` is recorded but
    must NEVER be called on a create path (rollback-by-reobservation invariant)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create_issue(self, fields):
        self.calls.append(("create_issue",))
        return {"key": "NEW-1", "id": "1001"}

    def update_issue(self, key, **fields):
        self.calls.append(("update_issue", key))
        return {"key": key}

    def delete_issue(self, key):
        self.calls.append(("delete_issue", key))
        return {"key": key}

    def add_label(self, key, label):
        self.calls.append(("add_label", key, label))

    def set_entity_property(self, key, name, value):
        self.calls.append(("set_entity_property", key, name))

    def search_issues(self, jql, *a, **k):
        self.calls.append(("search_issues",))
        return []

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def count(self, method: str) -> int:
        return sum(1 for c in self.calls if c[0] == method)


@pytest.mark.parametrize("route", [None, "legacy"])
def test_s7_create_route_creates_before_label_and_never_deletes(tmp_path, monkeypatch, route):
    """The create route (coordinated default AND legacy rollback core) drives a create
    end-to-end against a fake backend with the observable ordering: the issue is CREATED
    before any label is added, and ``delete_issue`` is NEVER called."""
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    fake = _RecordingCreateClient()
    store = BindingStore(tmp_path / f".bind-{route}")
    ctx = BatchApplyContext(
        client=fake, repo_root=tmp_path, pass_id="s5-create", binding_store=store
    )
    create = {
        "action": "create",
        "local_id": f"L-{route}",
        "fields": {"summary": "Reconcile", "issuetype": {"name": "Task"}},
    }
    dispatch_mutation(create, ctx)

    names = fake.names()
    assert fake.count("create_issue") == 1
    assert "add_label" in names, "the create must add the rebar-id identity label"
    assert names.index("create_issue") < names.index("add_label"), "create must precede label"
    assert fake.count("delete_issue") == 0  # rollback-by-reobservation, never a remote delete


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 8 — RESTART / RESUME (observe-before-replay; no re-executed hop)
# ════════════════════════════════════════════════════════════════════════════════


def test_s8_replay_safety_forbids_replay_after_ambiguous_commit():
    """After an ambiguous commit, ``commit_unknown`` is REPLAY-FORBIDDEN (observe before
    replaying a non-idempotent write); ``decide_replay`` yields ``recovered`` (no replay)
    for an already-``desired`` observation and ``retryable_deferred`` for an
    ``old_conclusive`` observation with budget."""
    assert replay_safety_for(Disposition.commit_unknown) == ReplaySafety.forbidden
    assert replay_safety_for(Disposition.commit_unknown) != ReplaySafety.safe

    # Already at the desired state → recovered, and the replay flag is False (no re-write).
    disposition, do_replay = decide_replay(observation="desired", budget_remaining=True)
    assert disposition == Disposition.recovered
    assert do_replay is False
    # Old but conclusive with budget → retryable_deferred, replay permitted.
    disposition, do_replay = decide_replay(observation="old_conclusive", budget_remaining=True)
    assert disposition == Disposition.retryable_deferred
    assert do_replay is True
    # An inconclusive/failed observation → commit_unknown, replay withheld.
    disposition, do_replay = decide_replay(observation="inconclusive", budget_remaining=True)
    assert disposition == Disposition.commit_unknown
    assert do_replay is False


class _ResumeTransport:
    """A transport recording ``transition_issue_by_name`` calls; ``get_issue`` reports the
    CURRENT status so replay resumes from mid-trail (no already-applied hop is re-run)."""

    def __init__(self, current_status: str) -> None:
        self._current = current_status
        self.transitions: list[tuple[str, str]] = []

    def get_issue(self, remote_id: str) -> dict:
        return {"key": remote_id, "fields": {"status": {"name": self._current}}}

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None:
        self.transitions.append((remote_id, target_status))


def _write_status_event(ticket_dir: Path, ts: int, target: str, current: str) -> None:
    fname = f"{ts:020d}-uuid{ts:020d}-STATUS.json"
    (ticket_dir / fname).write_text(
        json.dumps({"data": {"status": target, "current_status": current}}),
        encoding="utf-8",
    )


def _make_tracker(root: Path, local_id: str, local_hops: list[str]) -> Path:
    tracker = root / "tracker"
    ticket_dir = tracker / local_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    prev = "open"
    for i, target in enumerate(local_hops):
        _write_status_event(ticket_dir, 100 + i * 100, target=target, current=prev)
        prev = target
    return tracker


def test_s8_transition_replay_resume_skips_already_applied_hops(tmp_path, monkeypatch):
    """A resume from a mid-trail point (current status ``In Progress`` on a recorded
    ``open → in_progress → closed`` trail) replays ONLY the remaining ``Done`` hop — the
    already-applied ``In Progress`` hop is NOT re-executed."""
    tracker = _make_tracker(tmp_path, "tkt-resume", ["open", "in_progress", "closed"])
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tracker))
    monkeypatch.setenv("REBAR_ROOT", str(tracker.parent))
    client = _ResumeTransport(current_status="In Progress")

    landed = replay_transition(client, "DIG-2", "tkt-resume", "Done")

    assert landed is True
    assert client.transitions == [("DIG-2", "Done")]  # the applied hop is not re-run


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 9 — PARTIAL OUTCOME (commit_unknown: own bucket, NON-replaying, not applied)
# ════════════════════════════════════════════════════════════════════════════════


def test_s9_commit_unknown_is_its_own_outcome_and_not_applied():
    """A ``commit_unknown`` disposition surfaces as a non-applied outcome (its own
    ``deferred`` bucket, ``is_success`` False) and is replay-FORBIDDEN — a partial /
    ambiguous write is never counted as applied nor blindly re-driven."""
    from rebar_reconciler import failure_policy

    assert failure_policy.bucket_for(Disposition.commit_unknown) == "deferred"
    assert failure_policy.is_success(Disposition.commit_unknown) is False
    assert failure_policy.bucket_for(Disposition.commit_unknown) != "applied"
    assert replay_safety_for(Disposition.commit_unknown) == ReplaySafety.forbidden
    # Distinct from the clean applied/recovered successes.
    assert failure_policy.is_success(Disposition.applied) is True
    assert failure_policy.is_success(Disposition.recovered) is True


# ════════════════════════════════════════════════════════════════════════════════
# Scenario 10 — DELIVERY FAILURE (observability sink fails open; delivery still lands)
# ════════════════════════════════════════════════════════════════════════════════


def test_s10_observability_sink_failure_never_costs_delivery(tmp_path, monkeypatch):
    """The delivery/observability layer failing must NOT abort a mutation that would
    otherwise land: a broken alert store raises inside the sink, yet
    ``record_parent_divergence`` fails OPEN (returns None, no exception) AND a concurrent
    coordinator cutover still applies its mutation with an intact report."""

    class _BrokenAlertStore:
        def append(self, *_a, **_k):
            raise OSError("state dir unwritable")

    monkeypatch.setattr(pass_io, "_load_alert_store", lambda: _BrokenAlertStore())

    # The observability sink fails open — no exception escapes, nothing is delivery-cost.
    result = pass_io.record_parent_divergence(
        "outbound-parent-rejected",
        key="DIG-9",
        local_id="L-9",
        parent="DIG-1",
        exc=RuntimeError("parent rejected"),
        repo_root=tmp_path,
    )
    assert result is None

    # And a real coordinator cutover, run while that sink is broken, still delivers: the
    # mutation applies and the five-bucket report is intact.
    rec = RecordingAcli()
    cloud = cloud_backend(monkeypatch, rec)
    report, tally, sink = run_coordinator_cutover(cloud.transport, tmp_path, "DIG-9")
    assert report.outcome_for("DIG-9").bucket == "applied"
    assert tally["buckets"] == CLEAN_BUCKETS
    assert len(rec.workitem_edits) == 1  # the physical mutation still reached the wire
    assert not sink[0].get("error")
