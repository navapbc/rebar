"""Provider-neutral non-create coordinator over the S2 immutable plans.

``coordinate(ticket_plans, *, execute, budget_factory=None, locate=None)``
executes each mutate plan's atomic postconditions ONCE, in dependency order,
driving the injected ``execute`` adapter through the S1 ``RetryBudget`` for
transient retries.

Its DECISION logic performs ZERO I/O and reads no clock: identical inputs yield
equal outputs. The injected ``execute`` adapter is the SOLE side-effect channel,
and each atomic op gets a fresh ``RetryBudget`` from ``budget_factory``. On a
late failure it PRESERVES proven earlier postconditions and never replays the
compound mutation (AC2); it honors S2 pre-effect dispositions, enforces
inter-plan dependencies, isolates ticket-local failures, and stops a broad
authoritative failure only within its declared scope while independent tickets
continue (AC4/AC5).

Cross-sibling types are loaded by file path via the package's shared
``lazy_load`` idiom (``_loader.py``), matching every other reconciler sibling.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)
    lazy_load = sys.modules[_loader_key].lazy_load

_outcome_mod = lazy_load("rebar_reconciler.operation_outcome", "operation_outcome.py")
Disposition = _outcome_mod.Disposition
FailureScope = _outcome_mod.FailureScope

_budget_mod = lazy_load("rebar_reconciler.retry_budget", "retry_budget.py")
RetryBudget = _budget_mod.RetryBudget

_policy = lazy_load("rebar_reconciler.failure_policy", "failure_policy.py")


# ── Injected-adapter and result value types ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AtomicSignal:
    """What the injected ``execute`` adapter returns for one physical invocation.

    ``status`` is one of ``applied`` / ``already_satisfied`` / ``recovered`` /
    ``transient`` / ``permanent`` / ``unknown`` / ``skip``. Only ``transient`` is
    non-terminal (resolved here by the retry budget).
    """

    status: str
    scope: object = FailureScope.ticket
    diagnostic: str | None = None
    provider_delay_ms: int | None = None


@dataclass(frozen=True, slots=True)
class PostconditionOutcome:
    """One executed atomic postcondition's result. ``direction``/``action`` are the
    mutation enum ``.value``s."""

    direction: str
    action: str
    disposition: object


@dataclass(frozen=True, slots=True)
class TicketOutcome:
    """The per-ticket coordination result."""

    identity: str
    disposition: object
    failure_scope: object
    bucket: str
    postconditions: tuple
    diagnostics: tuple
    observation_version: object


@dataclass(frozen=True, slots=True)
class CoordinationReport:
    """The full coordination result: per-ticket outcomes, five-bucket tallies, and
    the recorded broad-scope stops as ``(scope_value, coord)`` tuples."""

    outcomes: tuple
    tallies: Mapping
    halted_scopes: tuple

    def outcome_for(self, identity: str):
        for outcome in self.outcomes:
            if outcome.identity == identity:
                return outcome
        raise KeyError(identity)


# ── Default injected collaborators (still side-effect free) ──────────────────────


class _NullClock:
    """A no-op clock for the default budget: now is 0 and sleep is discarded."""

    def now(self) -> int:
        return 0

    def sleep_ms(self, _ms: int) -> None:
        return None


def _default_budget_factory():
    return RetryBudget(clock=_NullClock(), jitter=lambda: 0.0)


def _empty_locate(_identity: str) -> Mapping:
    return {}


# ── Deterministic topological ordering ───────────────────────────────────────────


def _topological_order(plans) -> list[str]:
    """Kahn's algorithm over the in-set dependency edges, tie-broken by sorted
    identity; any cyclic remainder is appended in sorted-identity order."""
    ids = sorted(plan.identity for plan in plans)
    present = set(ids)
    deps = {plan.identity: sorted({d for d in plan.dependencies if d in present}) for plan in plans}
    indeg = {i: 0 for i in ids}
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for node in ids:
        for dep in deps[node]:
            adj[dep].append(node)
            indeg[node] += 1
    ready = sorted(i for i in ids if indeg[i] == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in sorted(adj[node]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    if len(order) < len(ids):
        placed = set(order)
        order.extend(i for i in ids if i not in placed)
    return order


# ── Per-atomic-op execution (the sole side-effect path) ──────────────────────────


def _run_atomic_op(plan, mutation, execute, budget_factory) -> tuple:
    """Invoke ``execute`` for one mutation, resolving transients through a fresh
    budget. Returns ``(disposition, scope)``."""
    budget = budget_factory()
    while True:
        signal = execute(plan, mutation)
        if signal.status != "transient":
            return _policy.status_to_disposition(signal.status), signal.scope
        retry = budget.attempt_retry(provider_delay_ms=signal.provider_delay_ms)
        if retry.action == "retry":
            continue
        if retry.action == "defer":
            return Disposition.retryable_deferred, signal.scope
        return Disposition.exhausted_transient, signal.scope


def _combine_success(postconditions) -> object:
    """Combine all-success postconditions with precedence recovered > applied >
    already_satisfied; an empty tuple is ``applied``."""
    seen = {pc.disposition for pc in postconditions}
    if Disposition.recovered in seen:
        return Disposition.recovered
    if Disposition.applied in seen:
        return Disposition.applied
    if Disposition.already_satisfied in seen:
        return Disposition.already_satisfied
    return Disposition.applied


def _execute_plan(plan, execute, budget_factory) -> tuple:
    """Run a mutate plan's postconditions in order, stopping at the first non-success
    without replaying earlier applied ops. Returns
    ``(disposition, scope, postconditions)``."""
    postconditions: list = []
    for mutation in plan.mutations:
        disp, scope = _run_atomic_op(plan, mutation, execute, budget_factory)
        postconditions.append(
            PostconditionOutcome(mutation.direction.value, mutation.action.value, disp)
        )
        if not _policy.is_success(disp):
            return disp, scope, tuple(postconditions)
    return _combine_success(postconditions), FailureScope.none, tuple(postconditions)


# ── Per-plan outcome assembly ────────────────────────────────────────────────────


def _make_outcome(plan, disposition, scope, postconditions, diagnostics) -> TicketOutcome:
    return TicketOutcome(
        identity=plan.identity,
        disposition=disposition,
        failure_scope=scope,
        bucket=_policy.bucket_for(disposition),
        postconditions=tuple(postconditions),
        diagnostics=tuple(diagnostics),
        observation_version=plan.observation_version,
    )


def _halted_diagnostic(plan, stopped, locate) -> str | None:
    """If a prior broad stop covers this plan, return a diagnostic naming the stopped
    scope; otherwise ``None``."""
    coords = locate(plan.identity)
    for scope, coord in stopped:
        if scope == FailureScope["global"]:
            return "halted by global-scope failure"
        if coord is not None and coords.get(scope.value) == coord:
            return f"halted by {scope.value}-scope failure at {coord}"
    return None


def _has_blocked_dependency(plan, present, succeeded) -> bool:
    return any(dep in present and dep not in succeeded for dep in plan.dependencies)


def _build_outcome(plan, present, succeeded, stopped, execute, budget_factory, locate):
    if plan.disposition.value != "mutate":
        disp = _policy.defer_reason_to_disposition(plan.defer_reason)
        return _make_outcome(plan, disp, FailureScope.none, (), ())
    halt_diag = _halted_diagnostic(plan, stopped, locate)
    if halt_diag is not None:
        return _make_outcome(plan, Disposition.scope_deferred, FailureScope.none, (), (halt_diag,))
    if _has_blocked_dependency(plan, present, succeeded):
        return _make_outcome(plan, Disposition.dependency_deferred, FailureScope.none, (), ())
    disp, scope, postconditions = _execute_plan(plan, execute, budget_factory)
    return _make_outcome(plan, disp, scope, postconditions, ())


def _record_effects(outcome, succeeded, stopped, locate) -> None:
    """Register a mutate plan's outcome: success feeds ``succeeded``; a broad failure
    records a scope stop."""
    if _policy.is_success(outcome.disposition):
        succeeded.add(outcome.identity)
        return
    scope = outcome.failure_scope
    if not _policy.is_broad_scope(scope):
        return
    if scope == FailureScope["global"]:
        stopped.append((FailureScope["global"], None))
    else:
        stopped.append((scope, locate(outcome.identity).get(scope.value)))


# ── Entry point ──────────────────────────────────────────────────────────────────


def coordinate(ticket_plans, *, execute, budget_factory=None, locate=None) -> CoordinationReport:
    """Execute each mutate plan's atomic postconditions once, in dependency order,
    isolating ticket-local failures and honoring broad-scope stops."""
    plans = list(ticket_plans)
    if budget_factory is None:
        budget_factory = _default_budget_factory
    if locate is None:
        locate = _empty_locate
    by_id = {plan.identity: plan for plan in plans}
    present = set(by_id)
    succeeded: set[str] = set()
    stopped: list[tuple] = []
    outcomes: list[TicketOutcome] = []
    for identity in _topological_order(plans):
        plan = by_id[identity]
        outcome = _build_outcome(plan, present, succeeded, stopped, execute, budget_factory, locate)
        outcomes.append(outcome)
        if plan.disposition.value == "mutate":
            _record_effects(outcome, succeeded, stopped, locate)
    tallies = _policy.tally(outcomes)
    halted = tuple((scope.value, coord) for scope, coord in stopped)
    return CoordinationReport(tuple(outcomes), tallies, halted)
