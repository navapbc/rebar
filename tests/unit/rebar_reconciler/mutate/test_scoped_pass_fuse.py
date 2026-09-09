"""Finite-pass endpoint and provider fuse contracts.

Only retry-budget exhaustion dispositions increment a fuse. An endpoint opens after
three consecutive eligible outcomes across two tickets. A provider also requires two
endpoints. Matching success resets its scope, neutral outcomes do not reset or
increment, and unresolved bindings never share a scope. Open decisions expose their
scope, reason, identity, and retry time from the injected clock without performing I/O.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
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
    return _load("operation_outcome_fuse_test", "operation_outcome.py")


@pytest.fixture(scope="module")
def policy_mod():
    return _load("failure_policy_fuse_test", "failure_policy.py")


@pytest.fixture(scope="module")
def fuse_mod():
    return _load("pass_fuse_fuse_test", "pass_fuse.py")


@pytest.fixture(scope="module")
def mutation_mod():
    return _load("mutation_fuse_test", "mutation.py")


@pytest.fixture(scope="module")
def ticket_plan_mod():
    return _load("ticket_plan_fuse_test", "ticket_plan.py")


@pytest.fixture(scope="module")
def budget_mod():
    return _load("retry_budget_fuse_test", "retry_budget.py")


@pytest.fixture(scope="module")
def coordinator_mod():
    return _load("coordinator_fuse_test", "coordinator.py")


# ── Deterministic, injected test doubles ─────────────────────────────────────────


class _Outcome:
    """Minimal TicketOutcome shape whose string enums compare across module instances."""

    __slots__ = ("disposition", "failure_scope", "identity")

    def __init__(self, identity, disposition, failure_scope):
        self.identity = identity
        self.disposition = disposition
        self.failure_scope = failure_scope


def _outcome(outcome_mod, identity, disposition, scope="ticket"):
    return _Outcome(
        identity, getattr(outcome_mod.Disposition, disposition), outcome_mod.FailureScope[scope]
    )


def _locator(bindings):
    """Return a ``locate(identity) -> {"provider":..,"endpoint":..}`` closure."""

    def locate(identity):
        return bindings.get(identity, {})

    return locate


def _expected_rnb(now_ms: int, cooldown_ms: int) -> str:
    moment = datetime.fromtimestamp((now_ms + cooldown_ms) / 1000, tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


# Two tickets on the same endpoint of one provider; a third on a second endpoint.
_BINDINGS = {
    "T-1": {"provider": "jira", "endpoint": "https://a.example"},
    "T-2": {"provider": "jira", "endpoint": "https://a.example"},
    "T-3": {"provider": "jira", "endpoint": "https://b.example"},
    "T-4": {"provider": "jira", "endpoint": "https://a.example"},
    "O-1": {"provider": "gh", "endpoint": "https://z.example"},
}


class _Clock:
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


def _plan(ticket_plan_mod, identity, muts, *, dependencies=(), version="ov-1"):
    tp = ticket_plan_mod
    return tp.TicketPlan(
        identity=identity,
        mutations=tuple(muts),
        diagnostics=(),
        disposition=tp.PlanDisposition("mutate"),
        observation_version=version,
        payload={},
        dependencies=tuple(dependencies),
        defer_reason=None,
    )


class _ScriptedExecutor:
    """An injected ``execute(plan, mutation)`` adapter driven by a scripted table
    keyed by ``(identity, action)`` -> ``AtomicSignal`` or a per-invocation list."""

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


def _feed(fuse, outcomes):
    for outcome in outcomes:
        fuse.record(outcome)
    return fuse


# ════════════════════════════════════════════════════════════════════════════════
# HAPPY PATH — minimal executable spec handed to the implementer.
# ════════════════════════════════════════════════════════════════════════════════


def test_fuse_eligibility_predicate(policy_mod, outcome_mod):
    """Only the two budget-cap dispositions are fuse-eligible; every other disposition
    (successes, permanent failure, opaque commit-unknown, skipped) is excluded."""
    D = outcome_mod.Disposition
    assert policy_mod.is_fuse_eligible(D.exhausted_transient)
    assert policy_mod.is_fuse_eligible(D.retryable_deferred)
    for name in (
        "applied",
        "already_satisfied",
        "recovered",
        "permanent_failure",
        "commit_unknown",
        "skipped",
        "dependency_deferred",
        "scope_deferred",
        "safety_aborted",
    ):
        assert not policy_mod.is_fuse_eligible(getattr(D, name))


def test_endpoint_opens_after_three_eligible_two_tickets(fuse_mod, outcome_mod):
    """AC1: an endpoint opens after THREE consecutive eligible outcomes spanning at
    least TWO distinct tickets; the decision names the endpoint scope, a reason, and an
    exact ``retry_not_before``."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS), now_ms=0, cooldown_ms=60000)
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    decision = fuse.decision_for("T-1")
    assert decision is not None
    assert decision.scope == "endpoint"
    assert decision.reason
    assert decision.retry_not_before == _expected_rnb(0, 60000)


def test_below_threshold_no_decision(fuse_mod, outcome_mod):
    """Two eligible outcomes (one short of the threshold) do not open the endpoint."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1") is None


def test_matching_success_resets_consecutive_state(fuse_mod, outcome_mod):
    """AC3: a matching success resets the endpoint's consecutive run, so two eligible +
    a success + two eligible does NOT reach the threshold."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "applied"),  # resets the endpoint
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1") is None


def test_provider_requires_two_distinct_endpoints(fuse_mod, outcome_mod):
    """Provider scope opens only after eligible failures span two endpoints."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),  # endpoint a
            _outcome(outcome_mod, "T-2", "exhausted_transient"),  # endpoint a
            _outcome(outcome_mod, "T-4", "exhausted_transient"),  # endpoint a
        ],
    )
    # endpoint a is open, but the provider is not (single endpoint).
    assert fuse.decision_for("T-1").scope == "endpoint"
    assert fuse.decision_for("T-3") is None  # endpoint b independent, provider not open

    # endpoint b now contributes -> provider spans 2 distinct endpoints.
    fuse.record(_outcome(outcome_mod, "T-3", "exhausted_transient"))
    provider_decision = fuse.decision_for("T-3")
    assert provider_decision is not None
    assert provider_decision.scope == "provider"


def test_independent_scope_continues(fuse_mod, outcome_mod):
    """AC5: opening one endpoint leaves an unrelated provider's endpoint untouched — its
    identities get NO decision."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1") is not None
    assert fuse.decision_for("O-1") is None  # different provider + endpoint


# ════════════════════════════════════════════════════════════════════════════════
# ── S3T2 HELDOUT-START ── edge + E2E oracle withheld from the implementer ────────
# ════════════════════════════════════════════════════════════════════════════════


def test_three_eligible_one_ticket_does_not_open(fuse_mod, outcome_mod):
    """AC1 diversity: three eligible outcomes on a SINGLE ticket never reach the
    two-distinct-ticket requirement, so the endpoint stays closed."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1") is None


def test_retryable_deferred_is_eligible(fuse_mod, outcome_mod):
    """The cumulative-sleep-cap disposition (``retryable_deferred``) is eligible and can
    open an endpoint alongside ``exhausted_transient``."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "retryable_deferred"),
            _outcome(outcome_mod, "T-2", "retryable_deferred"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1") is not None


@pytest.mark.parametrize(
    "excluded",
    [
        "permanent_failure",
        "commit_unknown",
        "skipped",
        "dependency_deferred",
        "scope_deferred",
        "safety_aborted",
    ],
)
def test_excluded_dispositions_never_increment(fuse_mod, outcome_mod, excluded):
    """Excluded dispositions neither increment nor reset the fuse."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", excluded),
            _outcome(outcome_mod, "T-2", excluded),
            _outcome(outcome_mod, "T-1", excluded),
        ],
    )
    assert fuse.decision_for("T-1") is None


@pytest.mark.parametrize("success", ["applied", "already_satisfied", "recovered"])
def test_success_dispositions_reset_and_never_increment(fuse_mod, outcome_mod, success):
    """AC3/AC4: each success disposition (applied / already_satisfied / recovered) both
    fails to increment AND resets a partial run — two eligible then a success then two
    eligible stays below threshold."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", success),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1") is None


def test_neutral_outcome_does_not_break_consecutive_run(fuse_mod, outcome_mod):
    """A neutral outcome does not interrupt an eligible consecutive run."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "permanent_failure"),  # neutral, no reset
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1") is not None


def test_provider_reset_on_success_clears_distinct_endpoints(fuse_mod, outcome_mod):
    """A provider-scope success clears the accrued distinct-endpoint diversity so a later
    partial run cannot re-use stale endpoint breadth to open the provider early."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),  # ep a
            _outcome(outcome_mod, "T-3", "exhausted_transient"),  # ep b
            _outcome(outcome_mod, "T-1", "recovered"),  # success resets endpoint a + provider
        ],
    )
    # Provider diversity was reset; a fresh single-endpoint run must not open the provider.
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-4", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1").scope == "endpoint"  # endpoint a reopened
    assert fuse.decision_for("T-3") is None  # provider not open; ep b independent & idle


def test_decision_fields_are_exact(fuse_mod, outcome_mod):
    """AC6: the endpoint decision reports the exact scope value, a stable reason, the
    resolved endpoint identity, and a ``retry_not_before`` = ``now_ms + cooldown_ms``."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS), now_ms=1_000, cooldown_ms=90_000)
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    decision = fuse.decision_for("T-1")
    assert decision.scope == "endpoint"
    assert decision.endpoint == "https://a.example"
    assert decision.retry_not_before == _expected_rnb(1_000, 90_000)
    # A provider decision names the provider identity and provider scope.
    fuse.record(_outcome(outcome_mod, "T-3", "exhausted_transient"))
    prov = fuse.decision_for("T-3")
    assert prov.scope == "provider"
    assert prov.provider == "jira"


def test_decision_is_deterministic(fuse_mod, outcome_mod):
    """The decision logic reads no clock and does no I/O: the identical sequence fed to
    two independently-constructed fuses yields byte-identical decision fields."""
    seq = [
        ("T-1", "exhausted_transient"),
        ("T-2", "exhausted_transient"),
        ("T-1", "exhausted_transient"),
    ]

    def run():
        fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS), now_ms=5, cooldown_ms=42)
        _feed(fuse, [_outcome(outcome_mod, i, d) for i, d in seq])
        dec = fuse.decision_for("T-1")
        return (dec.scope, dec.reason, dec.retry_not_before, dec.endpoint, dec.provider)

    assert run() == run()


# ── E2E: real coordinator outcomes feed the fuse (budget-exhaustion provenance) ──


def _coord_report(coordinator_mod, budget_mod, plans, script):
    execute = _ScriptedExecutor(coordinator_mod, script)
    report = coordinator_mod.coordinate(
        plans,
        execute=execute,
        budget_factory=_budget_factory(budget_mod),
        locate=_locator(_BINDINGS),
    )
    return report, execute


def test_e2e_budget_exhaustion_counts_first_attempt_transient_absorbed(
    fuse_mod, coordinator_mod, budget_mod, mutation_mod, ticket_plan_mod
):
    """Only terminal budget exhaustion feeds the fuse, while recovery resets it."""
    trans = coordinator_mod.AtomicSignal(status="transient")
    applied = coordinator_mod.AtomicSignal(status="applied")
    plans = [
        _plan(ticket_plan_mod, "T-1", [_mut(mutation_mod, "outbound", "update", "T-1")]),
        _plan(ticket_plan_mod, "T-2", [_mut(mutation_mod, "outbound", "update", "T-2")]),
        _plan(ticket_plan_mod, "T-4", [_mut(mutation_mod, "outbound", "update", "T-4")]),
    ]
    script = {
        ("T-1", "update"): trans,  # always transient -> exhausted_transient (eligible)
        ("T-2", "update"): trans,
        ("T-4", "update"): [trans, applied],  # first-attempt transient absorbed -> applied
    }
    report, _ = _coord_report(coordinator_mod, budget_mod, plans, script)
    # Sanity: the two always-transient tickets exhausted; the absorbed one applied.
    assert report.outcome_for("T-1").disposition.value == "exhausted_transient"
    assert report.outcome_for("T-4").disposition.value == "applied"

    exhausted = report.outcome_for("T-1").disposition  # the eligible budget-cap disposition
    scope = report.outcome_for("T-1").failure_scope

    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(fuse, report.outcomes)
    # T-1 + T-2 are two eligible on endpoint a; the absorbed T-4 (applied) RESET it.
    assert fuse.decision_for("T-1") is None
    # Three genuine post-reset exhaustions across two tickets open the endpoint —
    # proving the absorbed first-attempt transient (T-4 applied) was never counted.
    fuse.record(_Outcome("T-1", exhausted, scope))
    fuse.record(_Outcome("T-2", exhausted, scope))
    fuse.record(_Outcome("T-1", exhausted, scope))
    assert fuse.decision_for("T-1").scope == "endpoint"


def test_e2e_permanent_failure_and_recovered_never_open(
    fuse_mod, coordinator_mod, budget_mod, mutation_mod, ticket_plan_mod
):
    """E2E exclusion: real permanent-failure and recovered outcomes never open a scope,
    even three-deep across two tickets."""
    perm = coordinator_mod.AtomicSignal(status="permanent")
    plans = [
        _plan(ticket_plan_mod, "T-1", [_mut(mutation_mod, "outbound", "update", "T-1")]),
        _plan(ticket_plan_mod, "T-2", [_mut(mutation_mod, "outbound", "update", "T-2")]),
        _plan(ticket_plan_mod, "T-4", [_mut(mutation_mod, "outbound", "update", "T-4")]),
    ]
    script = {("T-1", "update"): perm, ("T-2", "update"): perm, ("T-4", "update"): perm}
    report, _ = _coord_report(coordinator_mod, budget_mod, plans, script)
    assert all(o.disposition.value == "permanent_failure" for o in report.outcomes)
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(fuse, report.outcomes)
    assert fuse.decision_for("T-1") is None


def test_e2e_open_endpoint_defers_matching_but_independent_endpoint_runs(
    fuse_mod, coordinator_mod, budget_mod, mutation_mod, ticket_plan_mod
):
    """An open endpoint defers matching plans while an independent endpoint runs."""
    trans = coordinator_mod.AtomicSignal(status="transient")
    prime = [
        _plan(ticket_plan_mod, "T-1", [_mut(mutation_mod, "outbound", "update", "T-1")]),
        _plan(ticket_plan_mod, "T-2", [_mut(mutation_mod, "outbound", "update", "T-2")]),
        _plan(ticket_plan_mod, "T-4", [_mut(mutation_mod, "outbound", "update", "T-4")]),
    ]
    script = {k: trans for k in (("T-1", "update"), ("T-2", "update"), ("T-4", "update"))}
    report, _ = _coord_report(coordinator_mod, budget_mod, prime, script)
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(fuse, report.outcomes)
    assert fuse.decision_for("T-1").scope == "endpoint"  # endpoint a is open

    # A second wave: T-4 (endpoint a, matching -> deferred) and O-1 (endpoint z, runs).
    executed: list[str] = []
    deferred: list[str] = []
    for identity in ("T-4", "O-1"):
        if fuse.decision_for(identity) is not None:
            deferred.append(identity)
        else:
            executed.append(identity)
    assert deferred == ["T-4"]  # matching endpoint-a plan is held back
    assert executed == ["O-1"]  # independent endpoint continues


def test_success_after_open_recloses_scope(fuse_mod, outcome_mod):
    """A matching success closes an already open endpoint scope."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1").scope == "endpoint"  # opened
    fuse.record(_outcome(outcome_mod, "T-1", "applied"))  # proven success on endpoint a
    assert fuse.decision_for("T-1") is None  # re-closed by the matching success


def test_unresolvable_binding_never_opens_or_conflates(fuse_mod, outcome_mod):
    """Unresolved bindings never accumulate in a shared fuse scope."""
    fuse = fuse_mod.PassFuse(locate=_locator({}))  # every binding empty
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "U-1", "exhausted_transient"),
            _outcome(outcome_mod, "U-2", "exhausted_transient"),
            _outcome(outcome_mod, "U-3", "exhausted_transient"),
        ],
    )
    for ident in ("U-1", "U-2", "U-3"):
        assert fuse.decision_for(ident) is None


def test_retry_not_before_is_exact_literal(fuse_mod, outcome_mod):
    """retry_not_before matches the literal RFC 3339 instant from injected time."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS), now_ms=0, cooldown_ms=60000)
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1").retry_not_before == "1970-01-01T00:01:00Z"


def test_default_now_ms_is_not_epoch_zero(fuse_mod, outcome_mod):
    """The default clock produces a future instant instead of the epoch-zero sentinel."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    assert not fuse.decision_for("T-1").retry_not_before.startswith("1970-")


def test_both_open_prefers_provider_scope(fuse_mod, outcome_mod):
    """When an identity's endpoint scope AND provider scope are both open, ``decision_for``
    returns the BROADER provider decision (precedence), not the endpoint one."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS))
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),  # ep a
            _outcome(outcome_mod, "T-2", "exhausted_transient"),  # ep a -> endpoint a opens
            _outcome(outcome_mod, "T-1", "exhausted_transient"),  # ep a
            _outcome(outcome_mod, "T-3", "exhausted_transient"),  # ep b -> provider jira opens
        ],
    )
    # T-1 lives on endpoint a (open) of provider jira (open): provider wins.
    decision = fuse.decision_for("T-1")
    assert decision.scope == "provider"
    assert decision.provider == "jira"


# ════════════════════════════════════════════════════════════════════════════════
# Completed-run terminal outcomes verify that the fuse would not stop observed work.
# ════════════════════════════════════════════════════════════════════════════════


def test_historical_replay_completed_slice_never_prematurely_stops(fuse_mod, outcome_mod):
    """Every prefix of the completed-run corpus leaves all scopes closed."""
    import json

    fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "reconciler"
        / "rp03_fuse_completed_slice.json"
    )
    data = json.loads(fixture.read_text())
    records = data["records"]
    assert data["run_count"] == 1200
    assert len(records) == 1200
    bindings = {
        r["identity"]: {"provider": r["provider"], "endpoint": r["endpoint"]} for r in records
    }
    fuse = fuse_mod.PassFuse(locate=_locator(bindings), now_ms=0)
    eligible_seen = 0
    for record in records:
        outcome = _Outcome(
            record["identity"],
            getattr(outcome_mod.Disposition, record["disposition"]),
            outcome_mod.FailureScope["ticket"],
        )
        fuse.record(outcome)
        if record["disposition"] in ("exhausted_transient", "retryable_deferred"):
            eligible_seen += 1
        # No premature stop: this identity's scope is never open mid-replay.
        assert fuse.decision_for(record["identity"]) is None
    # The corpus is non-vacuous — it really does exercise fuse-eligible outcomes.
    assert eligible_seen > 0
    # And nothing is left open at the end either.
    for record in records:
        assert fuse.decision_for(record["identity"]) is None


def test_historical_replay_counterfactual_burst_would_stop(fuse_mod, outcome_mod):
    """A three-event eligible control burst opens the endpoint fuse."""
    fuse = fuse_mod.PassFuse(locate=_locator(_BINDINGS), now_ms=0)
    _feed(
        fuse,
        [
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
            _outcome(outcome_mod, "T-2", "exhausted_transient"),
            _outcome(outcome_mod, "T-1", "exhausted_transient"),
        ],
    )
    assert fuse.decision_for("T-1") is not None


# ════════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════════
# ── S3T2 HELDOUT-END ─────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════
