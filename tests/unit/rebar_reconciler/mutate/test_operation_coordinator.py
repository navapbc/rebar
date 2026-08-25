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


# ════════════════════════════════════════════════════════════════════════════════
# RP-03 S3 T3 — cut over non-create routing (selector + coordinate_and_fuse).
#
# These pin the OBSERVABLE contract of the cutover surface added to
# ``rebar_reconciler.batch_dispatch``:
#   * ``route_for(action, overrides=None)`` — the internal per-family selector whose
#     six non-create families default to the ``coordinator`` route with ``legacy`` as
#     the internal rollback value (AC1; never dual-send: exactly one route returned).
#   * ``coordinate_and_fuse(ticket_plans, *, execute, locate, budget_factory, now_ms,
#     cooldown_ms)`` — runs the S3 T1 coordinator then the S3 T2 fuse over its
#     normalized terminal outcomes, projecting the five AC buckets, attaching a
#     ``FuseDecision`` (exact scope / reason / retry_not_before) to remaining matching
#     work once a scope opens while independent scopes continue (AC2/AC3/AC4/AC5),
#     and exposing an exact 5-bucket tally + a degraded flag.
#   * ``build_pass_tally(report)`` — projects the 5-bucket report onto the
#     applied/failed/deferred/skipped/recovered counts the LIVE pass tally consumes.
# Assertions are OBSERVABLE ONLY (route strings / bucket strings / decision fields /
# counts).
# ════════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def batch_dispatch_mod():
    return _load("batch_dispatch_t3_test", "batch_dispatch.py")


# Two tickets on endpoint-a of provider jira, a third on endpoint-b, plus an
# independent github endpoint — mirrors the fuse oracle's binding shape.
_T3_BINDINGS = {
    "T-1": {"provider": "jira", "endpoint": "https://a.example"},
    "T-2": {"provider": "jira", "endpoint": "https://a.example"},
    "T-3": {"provider": "jira", "endpoint": "https://a.example"},
    "T-4": {"provider": "jira", "endpoint": "https://a.example"},
    "O-1": {"provider": "github", "endpoint": "https://z.example"},
}


def _t3_locate(bindings):
    def locate(identity):
        return bindings.get(identity, {})

    return locate


def _always_transient(coordinator_mod):
    """An execute adapter whose every physical invocation signals a transient — so the
    coordinator's RetryBudget exhausts it to ``exhausted_transient`` (fuse-eligible)."""

    def execute(_plan, _mutation):
        return coordinator_mod.AtomicSignal(status="transient")

    return execute


# ── HAPPY PATH (handed to the implementer) ───────────────────────────────────────


def test_route_for_defaults_to_coordinator_for_non_create_families(batch_dispatch_mod):
    """AC1: every non-create family defaults to the ``coordinator`` route; ``create``
    (and any unrecognized action) stays ``legacy``. Exactly one route per family."""
    bd = batch_dispatch_mod
    for family in ("update", "delete", "probe", "clean_label", "repair_property", "conflict"):
        assert family in bd.NON_CREATE_FAMILIES
        assert bd.route_for(family) == "coordinator"
    assert bd.route_for("create") == "legacy"
    assert bd.route_for("totally-unknown") == "legacy"


def test_coordinate_and_fuse_all_applied_matches_coordinator(
    batch_dispatch_mod, coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """Happy path: when every plan applies, ``coordinate_and_fuse`` reproduces the
    coordinator's five-bucket tally exactly, opens no fuse, and is not degraded."""
    plans = [
        _plan(ticket_plan_mod, "T-1", [_mut(mutation_mod, "outbound", "update", "T-1")]),
        _plan(ticket_plan_mod, "O-1", [_mut(mutation_mod, "outbound", "delete", "O-1")]),
    ]
    report = batch_dispatch_mod.coordinate_and_fuse(
        plans,
        execute=_ScriptedExecutor(coordinator_mod, {}),
        locate=_t3_locate(_T3_BINDINGS),
        budget_factory=_budget_factory(budget_mod),
        now_ms=0,
    )
    assert report.tallies == {
        "applied": 2,
        "recovered": 0,
        "deferred": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert report.fuse_decisions == ()
    assert report.degraded is False
    for identity in ("T-1", "O-1"):
        assert report.outcome_for(identity).bucket == "applied"
        assert report.outcome_for(identity).fuse_decision is None


# ════════════════════════════════════════════════════════════════════════════════
# ── S3T3-CUTOVER HELDOUT-START ── edge + E2E oracle withheld from the implementer ──
# ════════════════════════════════════════════════════════════════════════════════


def test_route_for_override_flips_one_family_without_dual_send(batch_dispatch_mod):
    """AC1: an ``overrides`` map flips a single family to its ``legacy`` rollback value
    and returns exactly that one route; every other family keeps ``coordinator`` (no
    dual-send — one active route per family)."""
    bd = batch_dispatch_mod
    overrides = {"delete": "legacy"}
    assert bd.route_for("delete", overrides) == "legacy"
    assert bd.route_for("update", overrides) == "coordinator"
    assert bd.route_for("probe", overrides) == "coordinator"


def test_route_for_rejects_unknown_route_value(batch_dispatch_mod):
    """A route value that is neither ``coordinator`` nor ``legacy`` is rejected — the
    selector can only ever yield one of the two known routes."""
    with pytest.raises(ValueError):
        batch_dispatch_mod.route_for("update", {"update": "shadow"})


def test_coordinate_and_fuse_opens_endpoint_and_defers_remaining(
    batch_dispatch_mod, coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """AC3/AC5: three tickets on one endpoint that each exhaust the retry budget open
    that endpoint's fuse; the fourth matching ticket is DEFERRED with the exact fuse
    scope / reason / retry_not_before, while the independent github ticket applies."""
    plans = [
        _plan(ticket_plan_mod, ident, [_mut(mutation_mod, "outbound", "update", ident)])
        for ident in ("T-1", "T-2", "T-3", "T-4")
    ]
    plans.append(_plan(ticket_plan_mod, "O-1", [_mut(mutation_mod, "outbound", "update", "O-1")]))
    report = batch_dispatch_mod.coordinate_and_fuse(
        plans,
        execute=_always_transient_or_apply(coordinator_mod),
        locate=_t3_locate(_T3_BINDINGS),
        budget_factory=_budget_factory(budget_mod),
        now_ms=0,
        cooldown_ms=60000,
    )
    # O-1 is on an independent endpoint/provider — never touched by the jira fuse.
    o1 = report.outcome_for("O-1")
    assert o1.bucket == "applied"
    assert o1.fuse_decision is None
    # T-1..T-3 tripped the fuse and are themselves failed (they exhausted the budget).
    for ident in ("T-1", "T-2", "T-3"):
        assert report.outcome_for(ident).bucket == "failed"
    # T-4 is the remaining matching work once the endpoint opened → deferred, carrying
    # the exact endpoint decision.
    t4 = report.outcome_for("T-4")
    assert t4.bucket == "deferred"
    assert t4.fuse_decision is not None
    assert t4.fuse_decision.scope == "endpoint"
    assert t4.fuse_decision.reason
    assert t4.fuse_decision.retry_not_before == "1970-01-01T00:01:00Z"
    # exactly one endpoint decision surfaced.
    assert len(report.fuse_decisions) == 1
    assert report.fuse_decisions[0].scope == "endpoint"
    assert report.degraded is True


def test_coordinate_and_fuse_independent_provider_never_conflated(
    batch_dispatch_mod, coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """AC3: a failing endpoint opening its fuse leaves an unrelated provider's ticket
    fully applied — scopes are isolated, never conflated."""
    plans = [
        _plan(ticket_plan_mod, ident, [_mut(mutation_mod, "outbound", "update", ident)])
        for ident in ("T-1", "T-2", "T-3")
    ]
    plans.append(_plan(ticket_plan_mod, "O-1", [_mut(mutation_mod, "outbound", "update", "O-1")]))

    # O-1 applies; the T-* all exhaust.
    def execute(plan, mutation):
        if plan.identity == "O-1":
            return coordinator_mod.AtomicSignal(status="applied")
        return coordinator_mod.AtomicSignal(status="transient")

    report = batch_dispatch_mod.coordinate_and_fuse(
        plans,
        execute=execute,
        locate=_t3_locate(_T3_BINDINGS),
        budget_factory=_budget_factory(budget_mod),
        now_ms=0,
    )
    assert report.outcome_for("O-1").bucket == "applied"
    assert report.outcome_for("O-1").fuse_decision is None


def test_coordinate_and_fuse_success_under_open_scope_is_not_deferred(
    batch_dispatch_mod, coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """A same-scope SUCCESS arriving after the fuse opened re-closes the scope and is
    reported as applied (never deferred) — proven health beats an open fuse."""
    # T-1..T-3 exhaust and open endpoint-a; T-4 (sorted last) then APPLIES.
    plans = [
        _plan(ticket_plan_mod, ident, [_mut(mutation_mod, "outbound", "update", ident)])
        for ident in ("T-1", "T-2", "T-3", "T-4")
    ]

    def execute(plan, mutation):
        if plan.identity == "T-4":
            return coordinator_mod.AtomicSignal(status="applied")
        return coordinator_mod.AtomicSignal(status="transient")

    report = batch_dispatch_mod.coordinate_and_fuse(
        plans,
        execute=execute,
        locate=_t3_locate(_T3_BINDINGS),
        budget_factory=_budget_factory(budget_mod),
        now_ms=0,
    )
    t4 = report.outcome_for("T-4")
    assert t4.bucket == "applied"
    assert t4.fuse_decision is None


def test_build_pass_tally_projects_five_buckets(
    batch_dispatch_mod, coordinator_mod, mutation_mod, ticket_plan_mod, budget_mod
):
    """AC4: ``build_pass_tally`` projects the five-bucket report onto the pass-tally
    shape — applied_count folds recovered into applied successes, failed_count is the
    failed bucket, and the deferred/skipped/recovered counts are exact."""
    plans = [
        _plan(ticket_plan_mod, ident, [_mut(mutation_mod, "outbound", "update", ident)])
        for ident in ("T-1", "T-2", "T-3", "T-4")
    ]
    plans.append(_plan(ticket_plan_mod, "O-1", [_mut(mutation_mod, "outbound", "update", "O-1")]))
    report = batch_dispatch_mod.coordinate_and_fuse(
        plans,
        execute=_always_transient_or_apply(coordinator_mod),
        locate=_t3_locate(_T3_BINDINGS),
        budget_factory=_budget_factory(budget_mod),
        now_ms=0,
    )
    tally = batch_dispatch_mod.build_pass_tally(report)
    # O-1 applied → applied_count 1; T-1..T-3 failed → failed_count 3; T-4 deferred.
    assert tally["applied_count"] == 1
    assert tally["failed_count"] == 3
    assert tally["deferred_count"] == 1
    assert tally["skipped_count"] == 0
    assert tally["recovered_count"] == 0
    assert tally["buckets"]["failed"] == 3


def _always_transient_or_apply(coordinator_mod):
    def execute(plan, mutation):
        if plan.identity == "O-1":
            return coordinator_mod.AtomicSignal(status="applied")
        return coordinator_mod.AtomicSignal(status="transient")

    return execute


def test_e2e_coordinate_and_fuse_consumes_real_plan_pass(
    batch_dispatch_mod, coordinator_mod, planner_mod, mutation_mod, budget_mod
):
    """E2E: ``coordinate_and_fuse`` consumes the ACTUAL immutable plans produced by the
    S2 ``ticket_planner.plan_pass`` and tallies them into the five buckets with no fuse
    trip when every plan applies."""
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
    _observation, plans = planner_mod.plan_pass(
        pass_id="pass-t3-e2e",
        local_snapshot={"REB-10": {}, "REB-11": {}},
        remote_snapshot={},
        binding_view={},
        mode="live",
        selection={"kind": "all", "ids": []},
        limits={"max_changes": 100},
        mutations=mutations,
    )
    report = batch_dispatch_mod.coordinate_and_fuse(
        plans,
        execute=_ScriptedExecutor(coordinator_mod, {}),
        locate=lambda _i: {},
        budget_factory=_budget_factory(budget_mod),
        now_ms=0,
    )
    assert report.tallies["applied"] == 2
    assert report.tallies["failed"] == 0
    assert report.degraded is False
    assert report.fuse_decisions == ()


# ════════════════════════════════════════════════════════════════════════════════
# ── S3T3-CUTOVER HELDOUT-END ──
# ════════════════════════════════════════════════════════════════════════════════
