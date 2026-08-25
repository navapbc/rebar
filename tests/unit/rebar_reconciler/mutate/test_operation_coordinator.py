"""Held-out behavioral oracle for RP-03 S3 T1 — provider-neutral non-create coordinator.

This oracle pins the OBSERVABLE contract of two new pure/policy modules that
coordinate atomic non-create postconditions (update / delete / probe /
clean_label / repair_property / conflict) over the S2 immutable plans:

``rebar_reconciler.failure_policy``
    The normalization policy. Projects the 11-member ``operation_outcome.Disposition``
    vocabulary onto the five exact AC6 outcome buckets, classifies broad vs
    ticket-local ``FailureScope``, maps adapter signal-statuses and S2 ``DeferReason``s
    onto dispositions, and classifies a raw HTTP status code (+ action) into a
    provider-neutral (signal-status, scope) pair — the ticket-local 404 / permanent
    4xx / idempotent already-gone delete rule (AC3/AC4). PURE: no I/O, no clock.

``rebar_reconciler.coordinator``
    ``coordinate(ticket_plans, *, execute, budget_factory=None, locate=None)`` —
    executes each mutate plan's atomic postconditions ONCE in dependency order,
    calling the injected ``execute`` adapter through the S1 ``RetryBudget`` for
    transient retries. On a late failure it PRESERVES proven earlier postconditions
    and never replays the compound mutation (AC2). It honors the S2 pre-effect
    dispositions, enforces inter-plan dependencies (a blocked prerequisite defers
    the dependent with no out-of-order mutation), isolates ticket-local failures,
    and stops a broad authoritative failure only within its declared scope while
    independent tickets/endpoints continue (AC4/AC5). Its DECISION logic performs
    zero I/O — the injected ``execute`` adapter is the sole side-effect channel.

Assertions are OBSERVABLE ONLY (enums / buckets / tuples / counts) — never private
names or source text — so a behavior-preserving refactor cannot break them.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"

if "rebar_reconciler" not in sys.modules:  # pragma: no cover - import bootstrap
    _pkg = types.ModuleType("rebar_reconciler")
    _pkg.__path__ = [str(RECON_DIR)]
    sys.modules["rebar_reconciler"] = _pkg


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECON_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outcome_mod():
    return _load("operation_outcome_coord_test", "operation_outcome.py")


@pytest.fixture(scope="module")
def mutation_mod():
    return _load("mutation_coord_test", "mutation.py")


@pytest.fixture(scope="module")
def ticket_plan_mod():
    return _load("ticket_plan_coord_test", "ticket_plan.py")


@pytest.fixture(scope="module")
def planner_mod():
    return _load("ticket_planner_coord_test", "ticket_planner.py")


@pytest.fixture(scope="module")
def budget_mod():
    return _load("retry_budget_coord_test", "retry_budget.py")


@pytest.fixture(scope="module")
def policy_mod():
    return _load("failure_policy_coord_test", "failure_policy.py")


@pytest.fixture(scope="module")
def coordinator_mod():
    return _load("coordinator_coord_test", "coordinator.py")


# ── Deterministic, injected test doubles ─────────────────────────────────────────


class _Clock:
    """A frozen clock: ``now`` never advances and ``sleep_ms`` is recorded, not slept.

    Injected into ``RetryBudget`` so the coordinator's retry path is deterministic and
    performs no real wall-clock I/O."""

    def __init__(self) -> None:
        self.slept: list[int] = []

    def now(self) -> int:
        return 0

    def sleep_ms(self, ms: int) -> None:
        self.slept.append(ms)


def _budget_factory(budget_mod):
    def factory():
        return budget_mod.RetryBudget(clock=_Clock(), jitter=lambda: 0.0)

    return factory


def _mut(mutation_mod, direction, action, target, payload=None):
    d = mutation_mod.MutationDirection
    a = mutation_mod.MutationAction
    return mutation_mod.Mutation(
        direction=getattr(d, direction),
        action=getattr(a, action),
        target=target,
        payload=payload or {},
        provenance={"src": direction},
    )


def _plan(
    ticket_plan_mod,
    identity,
    muts,
    *,
    disposition="mutate",
    dependencies=(),
    defer_reason=None,
    version="ov-1",
):
    tp = ticket_plan_mod
    disp = tp.PlanDisposition(disposition)
    reason = tp.DeferReason(defer_reason) if defer_reason is not None else None
    return tp.TicketPlan(
        identity=identity,
        mutations=tuple(muts),
        diagnostics=(),
        disposition=disp,
        observation_version=version,
        payload={},
        dependencies=tuple(dependencies),
        defer_reason=reason,
    )


class _ScriptedExecutor:
    """An injected ``execute(plan, mutation)`` adapter driven by a scripted table.

    ``script`` maps ``(identity, action)`` to an ``AtomicSignal`` (or a list of
    signals consumed one per physical invocation, to script transient→terminal
    sequences). Every physical invocation is recorded in ``calls`` so the oracle can
    assert exact call counts (postconditions executed ONCE; no compound replay)."""

    def __init__(self, coordinator_mod, script):
        self._c = coordinator_mod
        self._script = dict(script)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, plan, mutation):
        key = (plan.identity, mutation.action.value)
        self.calls.append(key)
        entry = self._script.get(key)
        if entry is None:
            return self._c.AtomicSignal(status="applied")
        if isinstance(entry, list):
            idx = min(sum(1 for c in self.calls if c == key) - 1, len(entry) - 1)
            return entry[idx]
        return entry


# ════════════════════════════════════════════════════════════════════════════════
# HAPPY PATH — minimal executable spec handed to the implementer.
# ════════════════════════════════════════════════════════════════════════════════


def test_bucket_projection_maps_success_dispositions(policy_mod, outcome_mod):
    """The 11→5 projection places the success dispositions in their exact buckets and
    exposes the canonical five-bucket vocabulary."""
    D = outcome_mod.Disposition
    assert tuple(policy_mod.OUTCOME_BUCKETS) == (
        "applied",
        "recovered",
        "deferred",
        "failed",
        "skipped",
    )
    assert policy_mod.bucket_for(D.applied) == "applied"
    assert policy_mod.bucket_for(D.already_satisfied) == "applied"
    assert policy_mod.bucket_for(D.recovered) == "recovered"
    assert policy_mod.is_success(D.applied)
    assert policy_mod.is_success(D.already_satisfied)
    assert policy_mod.is_success(D.recovered)
    assert not policy_mod.is_success(D.permanent_failure)


def test_broad_scope_classification(policy_mod, outcome_mod):
    """``ticket``/``none`` are local; ``endpoint``/``tenant``/``provider``/``global`` are
    broad (authoritative failures that can stop their declared scope)."""
    S = outcome_mod.FailureScope
    assert not policy_mod.is_broad_scope(S.ticket)
    assert not policy_mod.is_broad_scope(S.none)
    for name in ("endpoint", "tenant", "provider", "global"):
        assert policy_mod.is_broad_scope(S[name])


def test_coordinate_all_applied_happy_path(
    coordinator_mod, policy_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """Two independent update plans whose atomic ops all apply → each ticket is
    ``applied`` (bucket + disposition), postconditions ran ONCE, and the tallies count
    exactly the five buckets."""
    plans = [
        _plan(ticket_plan_mod, "REB-1", [_mut(mutation_mod, "outbound", "update", "REB-1")]),
        _plan(ticket_plan_mod, "REB-2", [_mut(mutation_mod, "outbound", "delete", "REB-2")]),
    ]
    execute = _ScriptedExecutor(coordinator_mod, {})
    report = coordinator_mod.coordinate(
        plans, execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    D = policy_mod  # alias for readability
    for identity in ("REB-1", "REB-2"):
        outcome = report.outcome_for(identity)
        assert outcome.bucket == "applied"
        assert D.is_success(outcome.disposition)
        assert len(outcome.postconditions) == 1
    # Each atomic op executed exactly once (no compound replay).
    assert execute.calls == [("REB-1", "update"), ("REB-2", "delete")]
    assert report.tallies == {
        "applied": 2,
        "recovered": 0,
        "deferred": 0,
        "failed": 0,
        "skipped": 0,
    }


def test_dependency_order_runs_prerequisite_before_dependent(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """A plan that declares a dependency on another in-set plan is executed AFTER its
    prerequisite; both apply."""
    plans = [
        _plan(
            ticket_plan_mod,
            "child",
            [_mut(mutation_mod, "outbound", "update", "child")],
            dependencies=("parent",),
        ),
        _plan(ticket_plan_mod, "parent", [_mut(mutation_mod, "outbound", "update", "parent")]),
    ]
    execute = _ScriptedExecutor(coordinator_mod, {})
    report = coordinator_mod.coordinate(
        plans, execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    # parent's op is invoked before child's op.
    assert execute.calls.index(("parent", "update")) < execute.calls.index(("child", "update"))
    assert report.outcome_for("parent").bucket == "applied"
    assert report.outcome_for("child").bucket == "applied"


def test_idempotent_already_gone_delete_is_applied(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """AC3: a delete whose target is already gone (adapter signals
    ``already_satisfied``) counts as idempotent success → ``applied`` bucket."""
    plans = [_plan(ticket_plan_mod, "GONE-1", [_mut(mutation_mod, "outbound", "delete", "GONE-1")])]
    execute = _ScriptedExecutor(
        coordinator_mod,
        {("GONE-1", "delete"): coordinator_mod.AtomicSignal(status="already_satisfied")},
    )
    report = coordinator_mod.coordinate(
        plans, execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    outcome = report.outcome_for("GONE-1")
    assert outcome.bucket == "applied"
    assert outcome.disposition.value == "already_satisfied"


def test_pre_effect_deferred_plan_is_honored_without_execution(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """A plan the S2 planner already marked non-``mutate`` (here ``defer`` +
    ``scope_deferred``) is honored as ``deferred`` WITHOUT any atomic execution."""
    plans = [
        _plan(
            ticket_plan_mod,
            "SCOPED-OUT",
            [_mut(mutation_mod, "outbound", "update", "SCOPED-OUT")],
            disposition="defer",
            defer_reason="scope_deferred",
        ),
    ]
    execute = _ScriptedExecutor(coordinator_mod, {})
    report = coordinator_mod.coordinate(
        plans, execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    outcome = report.outcome_for("SCOPED-OUT")
    assert outcome.bucket == "deferred"
    assert outcome.disposition.value == "scope_deferred"
    assert outcome.postconditions == ()
    assert execute.calls == []  # zero execution for a pre-effect-excluded plan


# ════════════════════════════════════════════════════════════════════════════════
# ── T3 HELDOUT-START ── edge + E2E oracle withheld from the implementer ──────────
# ════════════════════════════════════════════════════════════════════════════════


def test_full_disposition_bucket_projection(policy_mod, outcome_mod):
    """AC6: the WHOLE 11→5 projection is total and non-overlapping — every
    ``Disposition`` maps to exactly one of the five buckets, per the plan's table."""
    D = outcome_mod.Disposition
    expected = {
        D.applied: "applied",
        D.already_satisfied: "applied",
        D.recovered: "recovered",
        D.retryable_deferred: "deferred",
        D.dependency_deferred: "deferred",
        D.scope_deferred: "deferred",
        D.safety_aborted: "deferred",
        D.commit_unknown: "deferred",
        D.permanent_failure: "failed",
        D.exhausted_transient: "failed",
        D.skipped: "skipped",
    }
    # Every member is covered (guards against a new disposition slipping the projection).
    assert set(expected) == set(D)
    for disposition, bucket in expected.items():
        assert policy_mod.bucket_for(disposition) == bucket
        assert bucket in policy_mod.OUTCOME_BUCKETS


def test_defer_reason_maps_to_matching_disposition(policy_mod, ticket_plan_mod):
    """Each S2 ``DeferReason`` normalizes to the identically-named ``Disposition`` so a
    pre-effect-excluded plan lands in the right bucket (assert on the observable value +
    bucket, not on cross-module class identity)."""
    for reason in ticket_plan_mod.DeferReason:
        disposition = policy_mod.defer_reason_to_disposition(reason)
        assert disposition.value == reason.value
        assert policy_mod.bucket_for(disposition) in policy_mod.OUTCOME_BUCKETS


def test_late_failure_preserves_earlier_postcondition_and_never_replays(
    coordinator_mod, policy_mod, mutation_mod, ticket_plan_mod, budget_mod, outcome_mod
):
    """AC2: a plan whose FIRST atomic postcondition applies and whose SECOND fails
    permanently → the ticket is ``failed`` but the proven first postcondition is
    preserved, the failing op is NOT retried as a compound, and NO subsequent
    postcondition is attempted after the terminal failure (no out-of-order replay)."""
    # A single ticket carrying two ordered postconditions (update then delete).
    plan = _plan(
        ticket_plan_mod,
        "REB-9",
        [
            _mut(mutation_mod, "outbound", "update", "REB-9"),
            _mut(mutation_mod, "outbound", "delete", "REB-9"),
        ],
    )
    S = outcome_mod.FailureScope
    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("REB-9", "update"): coordinator_mod.AtomicSignal(status="applied"),
            ("REB-9", "delete"): coordinator_mod.AtomicSignal(status="permanent", scope=S.ticket),
        },
    )
    report = coordinator_mod.coordinate(
        [plan], execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    outcome = report.outcome_for("REB-9")
    assert outcome.bucket == "failed"
    assert outcome.disposition.value == "permanent_failure"
    # The proven earlier postcondition survives in the record...
    applied = [p for p in outcome.postconditions if policy_mod.is_success(p.disposition)]
    assert [(p.direction, p.action) for p in applied] == [("outbound", "update")]
    # ...and the failing op appears exactly once — no compound replay of the applied op.
    assert execute.calls.count(("REB-9", "update")) == 1
    assert execute.calls.count(("REB-9", "delete")) == 1


def test_ticket_local_404_isolates_ticket_others_continue(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod, outcome_mod
):
    """AC4/AC5: a ticket-scoped permanent failure (e.g. a stale-binding 404) fails ONLY
    its own ticket; an independent ticket in the same pass still applies."""
    S = outcome_mod.FailureScope
    plans = [
        _plan(ticket_plan_mod, "STALE", [_mut(mutation_mod, "outbound", "update", "STALE")]),
        _plan(ticket_plan_mod, "HEALTHY", [_mut(mutation_mod, "outbound", "update", "HEALTHY")]),
    ]
    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("STALE", "update"): coordinator_mod.AtomicSignal(status="permanent", scope=S.ticket),
        },
    )
    report = coordinator_mod.coordinate(
        plans, execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    assert report.outcome_for("STALE").bucket == "failed"
    assert report.outcome_for("STALE").failure_scope == S.ticket
    assert report.outcome_for("HEALTHY").bucket == "applied"


def test_broad_endpoint_failure_stops_only_its_endpoint(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod, outcome_mod
):
    """AC4: an authoritative endpoint-scoped failure halts the OTHER tickets on that
    endpoint (deferred, not executed), while tickets on a different endpoint continue."""
    S = outcome_mod.FailureScope
    # Two tickets on endpoint "A", one on endpoint "B".
    endpoints = {"A1": "A", "A2": "A", "B1": "B"}
    plans = [
        _plan(ticket_plan_mod, "A1", [_mut(mutation_mod, "outbound", "update", "A1")]),
        _plan(ticket_plan_mod, "A2", [_mut(mutation_mod, "outbound", "update", "A2")]),
        _plan(ticket_plan_mod, "B1", [_mut(mutation_mod, "outbound", "update", "B1")]),
    ]

    def locate(identity):
        return {"endpoint": endpoints[identity]}

    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("A1", "update"): coordinator_mod.AtomicSignal(status="permanent", scope=S.endpoint),
        },
    )
    report = coordinator_mod.coordinate(
        plans, execute=execute, budget_factory=_budget_factory(budget_mod), locate=locate
    )
    # A1 fails at endpoint scope; A2 (same endpoint) is halted → deferred, never executed.
    assert report.outcome_for("A1").bucket == "failed"
    assert report.outcome_for("A1").failure_scope == S.endpoint
    assert report.outcome_for("A2").bucket == "deferred"
    assert execute.calls.count(("A2", "update")) == 0
    # B1 on the independent endpoint applies.
    assert report.outcome_for("B1").bucket == "applied"
    assert execute.calls.count(("B1", "update")) == 1


def test_global_failure_halts_all_subsequent_tickets(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod, outcome_mod
):
    """A ``global``-scoped authoritative failure stops every later ticket regardless of
    endpoint."""
    S = outcome_mod.FailureScope
    plans = [
        _plan(ticket_plan_mod, "AAA", [_mut(mutation_mod, "outbound", "update", "AAA")]),
        _plan(ticket_plan_mod, "BBB", [_mut(mutation_mod, "outbound", "update", "BBB")]),
    ]
    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("AAA", "update"): coordinator_mod.AtomicSignal(status="permanent", scope=S["global"]),
        },
    )
    report = coordinator_mod.coordinate(
        [plans[0], plans[1]], execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    assert report.outcome_for("AAA").bucket == "failed"
    assert report.outcome_for("BBB").bucket == "deferred"
    assert execute.calls.count(("BBB", "update")) == 0


def test_blocked_prerequisite_defers_dependent_with_no_mutation(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod, outcome_mod
):
    """A dependent whose in-set prerequisite FAILED at runtime is deferred
    (``dependency_deferred``) and emits NO out-of-order mutation."""
    S = outcome_mod.FailureScope
    plans = [
        _plan(
            ticket_plan_mod,
            "dep",
            [_mut(mutation_mod, "outbound", "update", "dep")],
            dependencies=("pre",),
        ),
        _plan(ticket_plan_mod, "pre", [_mut(mutation_mod, "outbound", "update", "pre")]),
    ]
    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("pre", "update"): coordinator_mod.AtomicSignal(status="permanent", scope=S.ticket),
        },
    )
    report = coordinator_mod.coordinate(
        plans, execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    assert report.outcome_for("pre").bucket == "failed"
    dep = report.outcome_for("dep")
    assert dep.bucket == "deferred"
    assert dep.disposition.value == "dependency_deferred"
    assert dep.postconditions == ()
    assert execute.calls.count(("dep", "update")) == 0


def test_transient_exhaustion_via_budget_is_failed(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod, outcome_mod
):
    """A perpetually-transient op is retried through the S1 ``RetryBudget`` until the
    invocation budget is exhausted → ``exhausted_transient`` (failed bucket). The op is
    physically invoked exactly ``MAX_INVOCATIONS`` times."""
    S = outcome_mod.FailureScope
    plan = _plan(ticket_plan_mod, "FLAKY", [_mut(mutation_mod, "outbound", "update", "FLAKY")])
    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("FLAKY", "update"): coordinator_mod.AtomicSignal(status="transient", scope=S.ticket),
        },
    )
    report = coordinator_mod.coordinate(
        [plan], execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    outcome = report.outcome_for("FLAKY")
    assert outcome.bucket == "failed"
    assert outcome.disposition.value == "exhausted_transient"
    assert execute.calls.count(("FLAKY", "update")) == budget_mod.MAX_INVOCATIONS


def test_transient_then_success_recovers_within_budget(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod, outcome_mod
):
    """A transient failure that then succeeds on retry is a success within budget: the
    scripted [transient, applied] sequence yields an applied ticket after 2 invocations."""
    S = outcome_mod.FailureScope
    plan = _plan(ticket_plan_mod, "HEALS", [_mut(mutation_mod, "outbound", "update", "HEALS")])
    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("HEALS", "update"): [
                coordinator_mod.AtomicSignal(status="transient", scope=S.ticket),
                coordinator_mod.AtomicSignal(status="applied"),
            ],
        },
    )
    report = coordinator_mod.coordinate(
        [plan], execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    assert report.outcome_for("HEALS").bucket == "applied"
    assert execute.calls.count(("HEALS", "update")) == 2


def test_provider_delay_over_budget_defers_retryable(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod, outcome_mod
):
    """A transient signal carrying a provider delay that exceeds the cumulative-sleep
    budget defers with ``retryable_deferred`` (deferred bucket), not a failure."""
    S = outcome_mod.FailureScope
    plan = _plan(ticket_plan_mod, "SLOW", [_mut(mutation_mod, "outbound", "update", "SLOW")])
    over = budget_mod.MAX_CUMULATIVE_SLEEP_MS + 5000
    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("SLOW", "update"): coordinator_mod.AtomicSignal(
                status="transient", scope=S.ticket, provider_delay_ms=over
            ),
        },
    )
    report = coordinator_mod.coordinate(
        [plan], execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    outcome = report.outcome_for("SLOW")
    assert outcome.bucket == "deferred"
    assert outcome.disposition.value == "retryable_deferred"


def test_skip_signal_is_skipped_bucket(coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod):
    """An adapter ``skip`` signal (data condition — tombstone/index-lag) lands the
    ticket in the ``skipped`` bucket, distinct from deferred and failed."""
    plan = _plan(ticket_plan_mod, "TOMB", [_mut(mutation_mod, "outbound", "probe", "TOMB")])
    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("TOMB", "probe"): coordinator_mod.AtomicSignal(status="skip"),
        },
    )
    report = coordinator_mod.coordinate(
        [plan], execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    outcome = report.outcome_for("TOMB")
    assert outcome.bucket == "skipped"
    assert outcome.disposition.value == "skipped"


def test_recovered_op_is_recovered_bucket(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """An op whose drift was auto-healed (adapter signals ``recovered``) lands the ticket
    in the ``recovered`` bucket, distinct from plain ``applied``."""
    plan = _plan(ticket_plan_mod, "HEALED", [_mut(mutation_mod, "outbound", "update", "HEALED")])
    execute = _ScriptedExecutor(
        coordinator_mod,
        {
            ("HEALED", "update"): coordinator_mod.AtomicSignal(status="recovered"),
        },
    )
    report = coordinator_mod.coordinate(
        [plan], execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    assert report.outcome_for("HEALED").bucket == "recovered"


def test_classify_http_error_ticket_local_and_idempotent_delete(policy_mod, outcome_mod):
    """The provider-neutral HTTP classifier: a 404 on a DELETE is idempotent success
    (``already_satisfied``, ticket scope); a 404 on any other action and any other 4xx
    is a ticket-local permanent failure; 429/5xx are transient; all ticket-scoped."""
    S = outcome_mod.FailureScope
    assert policy_mod.classify_http_error(404, "delete") == ("already_satisfied", S.ticket)
    assert policy_mod.classify_http_error(404, "update") == ("permanent", S.ticket)
    assert policy_mod.classify_http_error(403, "update") == ("permanent", S.ticket)
    assert policy_mod.classify_http_error(429, "update") == ("transient", S.ticket)
    assert policy_mod.classify_http_error(503, "update") == ("transient", S.ticket)


def test_coordinate_is_deterministic_and_execute_is_sole_side_effect(
    coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """AC (zero-I/O decision logic): two runs over identical inputs produce equal reports
    (pure decision logic), and the injected ``execute`` adapter is the ONLY side-effect
    channel — its call sequence is identical and fully accounts for every invocation."""

    def build():
        return [
            _plan(ticket_plan_mod, "P1", [_mut(mutation_mod, "outbound", "update", "P1")]),
            _plan(
                ticket_plan_mod,
                "P2",
                [_mut(mutation_mod, "outbound", "delete", "P2")],
                dependencies=("P1",),
            ),
        ]

    exec1 = _ScriptedExecutor(coordinator_mod, {})
    exec2 = _ScriptedExecutor(coordinator_mod, {})
    r1 = coordinator_mod.coordinate(
        build(), execute=exec1, budget_factory=_budget_factory(budget_mod)
    )
    r2 = coordinator_mod.coordinate(
        build(), execute=exec2, budget_factory=_budget_factory(budget_mod)
    )
    assert r1.tallies == r2.tallies
    assert exec1.calls == exec2.calls
    # Every physical invocation is one of the plans' declared atomic ops (nothing else ran).
    assert set(exec1.calls) <= {("P1", "update"), ("P2", "delete")}


def test_e2e_coordinate_consumes_real_plan_pass_output(
    coordinator_mod, planner_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """E2E: the coordinator consumes the ACTUAL immutable ``TicketPlan`` tuple produced by
    the S2 ``ticket_planner.plan_pass`` (not hand-built plans) and applies each mutate
    plan's postconditions, tallying them into the five buckets."""
    d = mutation_mod.MutationDirection
    a = mutation_mod.MutationAction
    mutations = [
        mutation_mod.Mutation(
            direction=d.outbound,
            action=a.update,
            target="REB-10",
            payload={"summary": "x"},
            provenance={"src": "outbound"},
        ),
        mutation_mod.Mutation(
            direction=d.outbound,
            action=a.delete,
            target="REB-11",
            payload={},
            provenance={"src": "outbound"},
        ),
    ]
    observation, plans = planner_mod.plan_pass(
        pass_id="pass-e2e",
        local_snapshot={"REB-10": {}, "REB-11": {}},
        remote_snapshot={},
        binding_view={},
        mode="live",
        selection={"kind": "all", "ids": []},
        limits={"max_changes": 100},
        mutations=mutations,
    )
    execute = _ScriptedExecutor(coordinator_mod, {})
    report = coordinator_mod.coordinate(
        plans, execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    # Every real plan is a mutate plan; all ops apply.
    assert report.tallies["applied"] == 2
    assert report.tallies["failed"] == 0
    assert {o.identity for o in report.outcomes} == {"REB-10", "REB-11"}
    # The coordinator carries the plan's observation version onto its outcomes (traceable).
    for outcome in report.outcomes:
        assert outcome.observation_version == observation.version


def test_e2e_blocked_prereq_plan_is_deferred_by_coordinator(
    coordinator_mod, planner_mod, mutation_mod, budget_mod
):
    """E2E: when ``plan_pass`` itself defers a plan for an unsatisfiable prerequisite
    (``dependency_deferred`` pre-effect), the coordinator honors it as deferred and never
    executes it."""
    d = mutation_mod.MutationDirection
    a = mutation_mod.MutationAction
    # 'orphan' requires a create of 'missing' that is not planned this pass → S2 defers it.
    mutations = [
        mutation_mod.Mutation(
            direction=d.outbound,
            action=a.update,
            target="orphan",
            payload={"requires_create": "missing"},
            provenance={"src": "o"},
        ),
    ]
    _observation, plans = planner_mod.plan_pass(
        pass_id="pass-blocked",
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        mode="live",
        selection={"kind": "all", "ids": []},
        limits={"max_changes": 100},
        mutations=mutations,
    )
    execute = _ScriptedExecutor(coordinator_mod, {})
    report = coordinator_mod.coordinate(
        plans, execute=execute, budget_factory=_budget_factory(budget_mod)
    )
    outcome = report.outcome_for("orphan")
    assert outcome.bucket == "deferred"
    assert outcome.disposition.value == "dependency_deferred"
    assert execute.calls == []


# ════════════════════════════════════════════════════════════════════════════════
# ── T3 HELDOUT-END ──
# ════════════════════════════════════════════════════════════════════════════════
