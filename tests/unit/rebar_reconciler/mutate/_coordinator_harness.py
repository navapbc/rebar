"""Shared, credential-free harness for the RP-03 S5 coordinator taxonomy suites.

This module is imported (not collected) by the two coordinator taxonomy test files in
this directory:

* ``test_reconciler_coordinator.py`` — the ``coordinate_and_fuse`` bucket/fuse taxonomy
  (scenarios 1-6) plus venue parity;
* ``test_reconciler_coordinator_taxonomy.py`` — the create / restart / partial / delivery
  scenarios (7-10) whose real home is a layer around the coordinator.

It centralises the frozen-clock retry budget, the scripted ``execute`` double, the plan
builders, the ticket bindings, and the REAL Cloud/DC verified-fake wiring reused from
``tests/unit/rebar_reconciler/mutate/test_coordinator_venue_cutover.py``. Keeping it here
as a sibling ``*_harness.py`` — which pytest's prepend import mode puts on ``sys.path``,
and where the unit tier's ``rebar_reconciler`` engine bridge (``tests/unit/conftest.py`` +
``tests/unit/rebar_reconciler/conftest.py``) already resolves ``from rebar_reconciler import
…`` — lets both suites share ONE harness instead of inventing bespoke ones.

Everything is credential-free: no network, no real Jira, a frozen clock, no wall-clock
sleep.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from rebar_reconciler import coordinator as coordinator_mod
from rebar_reconciler import operation_outcome as outcome_mod
from rebar_reconciler.adapters.jira import acli
from rebar_reconciler.adapters.jira.backend import JiraBackend
from rebar_reconciler.adapters.jira_datacenter.backend import (
    JiraDataCenterBackend,
)
from rebar_reconciler.adapters.jira_datacenter.transport import (
    JiraDataCenterTransport,
)
from rebar_reconciler.apply_handlers import BatchApplyContext
from rebar_reconciler.batch_dispatch import (
    coordinate_and_fuse,
    make_coordinator_dispatch,
    make_guarded_execute,
    map_cutover_report,
    route_for,
)
from rebar_reconciler.mutation import (
    Mutation,
    MutationAction,
    MutationDirection,
)
from rebar_reconciler.retry_budget import RetryBudget
from rebar_reconciler.ticket_plan import PlanDisposition, TicketPlan

AtomicSignal = coordinator_mod.AtomicSignal
Disposition = outcome_mod.Disposition
FailureScope = outcome_mod.FailureScope
ReplaySafety = outcome_mod.ReplaySafety


class _FrozenClock:
    """A frozen clock for the coordinator's ``RetryBudget`` — no wall-clock I/O.

    ``now`` never advances and ``sleep_ms`` is recorded, not slept, so every retry /
    exhaustion path is deterministic (parity with the unit oracles' ``_Clock``)."""

    def __init__(self) -> None:
        self.slept: list[int] = []

    def now(self) -> int:
        return 0

    def sleep_ms(self, ms: int) -> None:
        self.slept.append(ms)


def budget_factory():
    def factory() -> RetryBudget:
        return RetryBudget(clock=_FrozenClock(), jitter=lambda: 0.0)

    return factory


def mut(action: str, target: str, payload: dict | None = None) -> Mutation:
    return Mutation(
        direction=MutationDirection.outbound,
        action=getattr(MutationAction, action),
        target=target,
        payload=payload or {},
        provenance={"src": "s5-taxonomy"},
    )


def plan(identity: str, muts=None, *, action: str = "update") -> TicketPlan:
    mutations = muts if muts is not None else [mut(action, identity)]
    return TicketPlan(
        identity=identity,
        mutations=tuple(mutations),
        diagnostics=(),
        disposition=PlanDisposition("mutate"),
        observation_version="ov-s5",
        payload={},
        dependencies=(),
        defer_reason=None,
    )


class ScriptedExecutor:
    """An injected ``execute(plan, mutation)`` adapter driven by a scripted table.

    ``script`` maps ``identity`` (or ``(identity, action)``) to an ``AtomicSignal`` or a
    list of signals consumed one-per-invocation. Every physical invocation is recorded so
    the taxonomy can assert exact call counts (no compound replay)."""

    def __init__(self, script: dict | None = None, default: str = "applied") -> None:
        self._script = dict(script or {})
        self._default = default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, ticket_plan, mutation):
        key = (ticket_plan.identity, mutation.action.value)
        self.calls.append(key)
        entry = self._script.get(key, self._script.get(ticket_plan.identity))
        if entry is None:
            return AtomicSignal(status=self._default)
        if isinstance(entry, list):
            idx = min(sum(1 for c in self.calls if c == key) - 1, len(entry) - 1)
            return entry[idx]
        return entry


def locate(bindings: dict):
    def _locate(identity: str) -> dict[str, Any]:
        return bindings.get(identity, {})

    return _locate


# Two provider families: several jira endpoints (for endpoint + provider fuses) and one
# independent github endpoint that must never be conflated with a jira fuse.
BINDINGS = {
    "T-1": {"provider": "jira", "endpoint": "https://a.example"},
    "T-2": {"provider": "jira", "endpoint": "https://a.example"},
    "T-3": {"provider": "jira", "endpoint": "https://a.example"},
    "T-4": {"provider": "jira", "endpoint": "https://a.example"},
    "J-A1": {"provider": "jira", "endpoint": "https://a.example"},
    "J-A2": {"provider": "jira", "endpoint": "https://a.example"},
    "J-B1": {"provider": "jira", "endpoint": "https://b.example"},
    "J-B2": {"provider": "jira", "endpoint": "https://b.example"},
    "J-C1": {"provider": "jira", "endpoint": "https://a.example"},
    "O-1": {"provider": "github", "endpoint": "https://z.example"},
    "G-1": {"provider": "github", "endpoint": "https://z.example"},
}


def run(plans, execute, *, cooldown_ms=None):
    """Drive ``coordinate_and_fuse`` with the shared frozen-clock budget + bindings."""
    return coordinate_and_fuse(
        plans,
        execute=execute,
        locate=locate(BINDINGS),
        budget_factory=budget_factory(),
        now_ms=0,
        cooldown_ms=cooldown_ms,
    )


# ── Venue verified fakes (reused from test_coordinator_venue_cutover.py) ──────────

SUMMARY = "S5 taxonomy coordinator summary"
CLEAN_BUCKETS = {"applied": 1, "recovered": 0, "deferred": 0, "failed": 0, "skipped": 0}


class RecordingAcli:
    """Records the ACLI argv the Cloud wire receives, stubbing the subprocess seam."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def run_acli(self, cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        self.argvs.append(list(cmd))
        key = cmd[cmd.index("--key") + 1] if "--key" in cmd else "DIG-1"
        return SimpleNamespace(stdout=json.dumps({"key": key}))

    @property
    def workitem_edits(self) -> list[list[str]]:
        return [cmd for cmd in self.argvs if cmd[:3] == ["jira", "workitem", "edit"]]


class _RecordingDCIssue:
    def __init__(self, key: str, field_edits: list[dict]) -> None:
        self.key = key
        self.raw = {"key": key, "fields": {}}
        self._field_edits = field_edits

    def update(self, fields: dict | None = None, **_kwargs: Any) -> None:
        self._field_edits.append(dict(fields or {}))
        self.raw["fields"].update(fields or {})


class RecordingDCClient:
    def __init__(self) -> None:
        self.field_edits: list[dict] = []
        self.issued: list[str] = []

    def issue(self, key: str) -> _RecordingDCIssue:
        self.issued.append(key)
        return _RecordingDCIssue(key, self.field_edits)


def summary_update_plan(key: str) -> TicketPlan:
    return plan(key, [mut("update", key, {"changed_fields": {"summary": SUMMARY}})])


def run_coordinator_cutover(client: Any, repo_root, key: str):
    """Drive the landed coordinator cutover for a summary UPDATE over a REAL venue wire.

    Production wiring (not a re-implementation): ``make_coordinator_dispatch`` →
    ``apply_handlers.dispatch_mutation`` → ``update_one`` → ``client.update_issue``."""
    assert route_for("update") == "coordinator"
    ctx = BatchApplyContext(client=client, repo_root=repo_root, pass_id="s5-pass")
    outcomes_sink: list[dict] = []
    dispatch_fn = make_coordinator_dispatch(ctx=ctx, outcomes_sink=outcomes_sink)
    execute = make_guarded_execute(
        abort_check=None,
        recheck_drift=lambda _c, _r, pin: pin,
        concurrency=None,
        repo_root=repo_root,
        head_pin_cell=["pin0"],
        dispatch_fn=dispatch_fn,
    )
    report = coordinate_and_fuse(
        [summary_update_plan(key)],
        execute=execute,
        locate=lambda _i: {"provider": "jira", "endpoint": "https://venue.example"},
        budget_factory=budget_factory(),
        now_ms=0,
    )
    return report, map_cutover_report(report), outcomes_sink


def cloud_backend(monkeypatch, rec: RecordingAcli):
    monkeypatch.setattr(acli.acli_subprocess, "_run_acli", rec.run_acli)
    client = acli.AcliClient(
        jira_url="https://example.atlassian.net",
        user="bot@example.com",
        api_token="t",
        jira_project="DIG",
    )
    return JiraBackend(transport=client)


def dc_backend(dc_client: RecordingDCClient):
    transport = JiraDataCenterTransport(client=dc_client, project="FAKE")
    return JiraDataCenterBackend(transport, client=dc_client)
