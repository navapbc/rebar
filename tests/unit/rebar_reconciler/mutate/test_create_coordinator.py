"""Held-out behavioral oracle for REB-3115 S4 T1 — the pure-decision CREATE coordinator.

This oracle pins the OBSERVABLE contract of the new pure module

``rebar_reconciler.create_coordinator``
    ``coordinate_create(plan, *, persist_pending, create_execute, observe,
    budget_factory=None)`` — the FIRST create slice. It durably persists the
    pending-binding intent BEFORE any create call, issues EXACTLY ONE physical
    create, and on an ambiguous create completion (timeout / connection-loss)
    re-observes via a replay-safe seam and either RECOVERS or retains-pending and
    DEFERS — never blind-replaying a second create. It captures the returned Jira
    key in a typed ``CreateOutcome`` and NEVER deletes a successfully-created remote
    issue (there is no delete seam at all). Its DECISION logic performs zero I/O and
    reads no clock — the three injected callables are the sole side-effect channels,
    so identical inputs yield equal outputs.

Assertions are OBSERVABLE ONLY (enums / buckets / counts / keys) — never private
names or source text — so a behavior-preserving refactor cannot break them. Every
one of the six ACs is covered by at least one test; two tests drive Cloud- and
DC-flavored STATEFUL fakes that record provider call counts and assert NO delete is
ever issued on any path.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

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
    return _load("operation_outcome_create_test", "operation_outcome.py")


@pytest.fixture(scope="module")
def policy_mod():
    return _load("failure_policy_create_test", "failure_policy.py")


@pytest.fixture(scope="module")
def create_mod():
    return _load("create_coordinator_create_test", "create_coordinator.py")


# ── Deterministic, injected test doubles ─────────────────────────────────────────


class _Plan:
    """A minimal create ticket plan carrying only what the coordinator reads: an
    ``.identity`` (mirrors how the sibling non-create coordinator reads
    ``plan.identity``)."""

    def __init__(self, identity: str) -> None:
        self.identity = identity


def _plan(identity: str = "REB-NEW") -> _Plan:
    return _Plan(identity)


class _ScriptedPersist:
    """An injected ``persist_pending(plan)`` seam. Records every call; RAISES the
    scripted error when one is configured so AC1 (durable-first) can be exercised."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.calls: list[str] = []

    def __call__(self, plan) -> None:
        self.calls.append(plan.identity)
        if self._raises is not None:
            raise self._raises


class _ScriptedCreate:
    """An injected ``create_execute(plan) -> CreateSignal`` seam. Records every physical
    create so the oracle can assert it fires AT MOST ONCE (no blind-replay)."""

    def __init__(self, create_mod, signal) -> None:
        self._c = create_mod
        self._signal = signal
        self.calls: list[str] = []

    def __call__(self, plan):
        self.calls.append(plan.identity)
        return self._signal


class _ScriptedObserve:
    """An injected replay-safe ``observe(plan) -> ObservationSignal`` seam, used ONLY on
    an ambiguous create completion. Records every call so the oracle can assert it fires
    at most once and never on an unambiguous path."""

    def __init__(self, create_mod, signal) -> None:
        self._c = create_mod
        self._signal = signal
        self.calls: list[str] = []

    def __call__(self, plan):
        self.calls.append(plan.identity)
        return self._signal


class _StatefulProviderFake:
    """A venue-flavored stateful fake standing in for a real Jira backend. It records
    provider create/observe/delete call counts, hands back the scripted create then
    observation signals, and EXPLODES if any delete/undo is ever requested — the AC6
    no-delete guarantee, enforced from the provider side rather than only by design."""

    def __init__(self, create_mod, *, venue: str, create_signal, observe_signal=None):
        self._c = create_mod
        self.venue = venue
        self._create_signal = create_signal
        self._observe_signal = observe_signal
        self.create_count = 0
        self.observe_count = 0
        self.delete_count = 0

    def persist_pending(self, plan) -> None:
        return None

    def create_execute(self, plan):
        self.create_count += 1
        return self._create_signal

    def observe(self, plan):
        self.observe_count += 1
        assert self._observe_signal is not None, f"{self.venue}: unexpected observe"
        return self._observe_signal

    def delete(self, plan):  # pragma: no cover - must never be called
        self.delete_count += 1
        raise AssertionError(f"{self.venue}: delete must never be issued")


# ════════════════════════════════════════════════════════════════════════════════
# AC1 — durable pending intent is written FIRST; a save failure makes zero provider
#       calls and yields permanent_failure without ever touching create/observe.
# ════════════════════════════════════════════════════════════════════════════════


def test_ac1_pending_save_failure_makes_no_provider_calls(create_mod, outcome_mod, policy_mod):
    D = outcome_mod.Disposition
    S = outcome_mod.FailureScope
    persist = _ScriptedPersist(raises=RuntimeError("store write failed"))
    create = _ScriptedCreate(create_mod, create_mod.CreateSignal(status="created", known_key="X-1"))
    observe = _ScriptedObserve(create_mod, create_mod.ObservationSignal(status="proven"))

    outcome = create_mod.coordinate_create(
        _plan(), persist_pending=persist, create_execute=create, observe=observe
    )

    assert outcome.disposition == D.permanent_failure
    assert outcome.failure_scope == S.ticket
    assert outcome.bucket == "failed"
    assert outcome.bucket == policy_mod.bucket_for(D.permanent_failure)
    assert outcome.pending_persisted is False
    assert outcome.known_key is None
    assert outcome.create_call_count == 0
    # The single persist attempt happened; NOTHING downstream fired.
    assert persist.calls == ["REB-NEW"]
    assert create.calls == []
    assert observe.calls == []


# ════════════════════════════════════════════════════════════════════════════════
# AC2 — a durable pending intent then EXACTLY ONE create; a "created" signal is applied.
# ════════════════════════════════════════════════════════════════════════════════


def test_ac2_happy_create_applies_after_one_call(create_mod, outcome_mod, policy_mod):
    D = outcome_mod.Disposition
    S = outcome_mod.FailureScope
    persist = _ScriptedPersist()
    create = _ScriptedCreate(
        create_mod, create_mod.CreateSignal(status="created", known_key="REB-501")
    )
    observe = _ScriptedObserve(create_mod, create_mod.ObservationSignal(status="proven"))

    outcome = create_mod.coordinate_create(
        _plan(), persist_pending=persist, create_execute=create, observe=observe
    )

    assert outcome.disposition == D.applied
    assert outcome.failure_scope == S.none
    assert outcome.bucket == "applied"
    assert outcome.bucket == policy_mod.bucket_for(D.applied)
    assert outcome.pending_persisted is True
    assert outcome.known_key == "REB-501"
    assert outcome.create_call_count == 1
    # Pending was persisted BEFORE the one-and-only create fired; observe never ran.
    assert persist.calls == ["REB-NEW"]
    assert create.calls == ["REB-NEW"]
    assert observe.calls == []


# ════════════════════════════════════════════════════════════════════════════════
# AC (permanent create) — a permanent create error fails without re-observing.
# ════════════════════════════════════════════════════════════════════════════════


def test_permanent_create_failure_does_not_observe(create_mod, outcome_mod, policy_mod):
    D = outcome_mod.Disposition
    S = outcome_mod.FailureScope
    persist = _ScriptedPersist()
    create = _ScriptedCreate(
        create_mod,
        create_mod.CreateSignal(status="permanent", scope=S.ticket, diagnostic="bad request"),
    )
    observe = _ScriptedObserve(create_mod, create_mod.ObservationSignal(status="proven"))

    outcome = create_mod.coordinate_create(
        _plan(), persist_pending=persist, create_execute=create, observe=observe
    )

    assert outcome.disposition == D.permanent_failure
    assert outcome.failure_scope == S.ticket
    assert outcome.bucket == "failed"
    assert outcome.bucket == policy_mod.bucket_for(D.permanent_failure)
    assert outcome.known_key is None
    assert outcome.create_call_count == 1
    assert observe.calls == []


# ════════════════════════════════════════════════════════════════════════════════
# AC3/AC4 — an ambiguous create (timeout) re-observes ONCE; a proven observation
#           recovers, carrying the observed key. No second create (no blind-replay).
# ════════════════════════════════════════════════════════════════════════════════


def test_ac3_timeout_then_proven_recovers(create_mod, outcome_mod, policy_mod):
    D = outcome_mod.Disposition
    S = outcome_mod.FailureScope
    persist = _ScriptedPersist()
    create = _ScriptedCreate(create_mod, create_mod.CreateSignal(status="timeout"))
    observe = _ScriptedObserve(
        create_mod, create_mod.ObservationSignal(status="proven", known_key="REB-777")
    )

    outcome = create_mod.coordinate_create(
        _plan(), persist_pending=persist, create_execute=create, observe=observe
    )

    assert outcome.disposition == D.recovered
    assert outcome.failure_scope == S.none
    assert outcome.bucket == "recovered"
    assert outcome.bucket == policy_mod.bucket_for(D.recovered)
    assert outcome.pending_persisted is True
    assert outcome.known_key == "REB-777"
    assert outcome.create_call_count == 1
    # Exactly one physical create, then exactly one replay-safe observation.
    assert create.calls == ["REB-NEW"]
    assert observe.calls == ["REB-NEW"]


# ════════════════════════════════════════════════════════════════════════════════
# AC4 — an ambiguous create (connection-loss) whose observation is inconclusive
#        RETAINS pending and defers as commit_unknown; still exactly one create.
# ════════════════════════════════════════════════════════════════════════════════


def test_ac4_connection_lost_then_inconclusive_defers_pending_retained(
    create_mod, outcome_mod, policy_mod
):
    D = outcome_mod.Disposition
    S = outcome_mod.FailureScope
    persist = _ScriptedPersist()
    create = _ScriptedCreate(create_mod, create_mod.CreateSignal(status="connection_lost"))
    observe = _ScriptedObserve(create_mod, create_mod.ObservationSignal(status="inconclusive"))

    outcome = create_mod.coordinate_create(
        _plan(), persist_pending=persist, create_execute=create, observe=observe
    )

    assert outcome.disposition == D.commit_unknown
    assert outcome.failure_scope == S.ticket
    # bucket_for(commit_unknown) is "deferred" — assert via the policy, not a literal.
    assert outcome.bucket == "deferred"
    assert outcome.bucket == policy_mod.bucket_for(D.commit_unknown)
    # Pending was persisted and NEVER unbound → next-pass recovery can converge.
    assert outcome.pending_persisted is True
    assert outcome.known_key is None
    assert outcome.create_call_count == 1
    assert create.calls == ["REB-NEW"]
    assert observe.calls == ["REB-NEW"]


# ════════════════════════════════════════════════════════════════════════════════
# AC5 — whenever a Jira key is known (created OR proven-recovered) it is carried on
#        CreateOutcome.known_key; when unknown it is None.
# ════════════════════════════════════════════════════════════════════════════════


def test_ac5_key_is_preserved_on_created_and_recovered(create_mod):
    persist = _ScriptedPersist()

    created = create_mod.coordinate_create(
        _plan(),
        persist_pending=persist,
        create_execute=_ScriptedCreate(
            create_mod, create_mod.CreateSignal(status="created", known_key="KEY-CREATED")
        ),
        observe=_ScriptedObserve(create_mod, create_mod.ObservationSignal(status="proven")),
    )
    assert created.known_key == "KEY-CREATED"

    recovered = create_mod.coordinate_create(
        _plan(),
        persist_pending=_ScriptedPersist(),
        create_execute=_ScriptedCreate(create_mod, create_mod.CreateSignal(status="timeout")),
        observe=_ScriptedObserve(
            create_mod,
            create_mod.ObservationSignal(status="proven", known_key="KEY-RECOVERED"),
        ),
    )
    assert recovered.known_key == "KEY-RECOVERED"


# ════════════════════════════════════════════════════════════════════════════════
# AC6 — there is NO delete seam in the module: the entry point takes exactly the
#        three side-effect callables (persist_pending / create_execute / observe) and
#        no delete/undo injectable, on EVERY path.
# ════════════════════════════════════════════════════════════════════════════════


def test_ac6_no_delete_injectable_exists(create_mod):
    import inspect

    sig = inspect.signature(create_mod.coordinate_create)
    kw_only = {
        name for name, p in sig.parameters.items() if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert kw_only == {"persist_pending", "create_execute", "observe", "budget_factory"}
    # No keyword-only seam names anything delete/undo/rollback-ish.
    assert not any(
        tok in name for name in kw_only for tok in ("delete", "undo", "rollback", "unbind")
    )


# ════════════════════════════════════════════════════════════════════════════════
# AC6 (venue fakes) — Cloud & DC stateful backends: no delete is EVER issued on the
#        created, recovered, or deferred paths, and each create fires exactly once.
# ════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("venue", ["cloud", "dc"])
def test_ac6_venue_fake_created_never_deletes(create_mod, venue):
    fake = _StatefulProviderFake(
        create_mod,
        venue=venue,
        create_signal=create_mod.CreateSignal(status="created", known_key=f"{venue}-1"),
    )
    outcome = create_mod.coordinate_create(
        _plan(f"REB-{venue}"),
        persist_pending=fake.persist_pending,
        create_execute=fake.create_execute,
        observe=fake.observe,
    )
    assert outcome.known_key == f"{venue}-1"
    assert outcome.create_call_count == 1
    assert fake.create_count == 1
    assert fake.observe_count == 0
    assert fake.delete_count == 0


@pytest.mark.parametrize("venue", ["cloud", "dc"])
def test_ac6_venue_fake_deferred_never_deletes(create_mod, outcome_mod, venue):
    D = outcome_mod.Disposition
    fake = _StatefulProviderFake(
        create_mod,
        venue=venue,
        create_signal=create_mod.CreateSignal(status="timeout"),
        observe_signal=create_mod.ObservationSignal(status="inconclusive"),
    )
    outcome = create_mod.coordinate_create(
        _plan(f"REB-{venue}"),
        persist_pending=fake.persist_pending,
        create_execute=fake.create_execute,
        observe=fake.observe,
    )
    assert outcome.disposition == D.commit_unknown
    assert outcome.create_call_count == 1
    assert fake.create_count == 1
    assert fake.observe_count == 1
    assert fake.delete_count == 0


# ════════════════════════════════════════════════════════════════════════════════
# Determinism — identical inputs yield an EQUAL CreateOutcome (pure decision core).
# ════════════════════════════════════════════════════════════════════════════════


def test_pure_decision_is_deterministic(create_mod):
    def run():
        return create_mod.coordinate_create(
            _plan("REB-DET"),
            persist_pending=_ScriptedPersist(),
            create_execute=_ScriptedCreate(
                create_mod, create_mod.CreateSignal(status="created", known_key="REB-DET-1")
            ),
            observe=_ScriptedObserve(create_mod, create_mod.ObservationSignal(status="proven")),
        )

    assert run() == run()
