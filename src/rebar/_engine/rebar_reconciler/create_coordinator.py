"""Provider-neutral pure-decision CREATE coordinator — the first create slice.

``coordinate_create(plan, *, persist_pending, create_execute, observe,
budget_factory=None)`` extends the S3 non-create coordinator's coordination to a
single outbound Jira CREATE. It is deliberately NARROW (REB-3115 S4 T1): it

1. durably persists the pending-binding INTENT *before* any create call
   (``persist_pending`` raises on failure → nothing downstream runs);
2. issues EXACTLY ONE physical create (``create_execute``) — never in a retry
   loop, because a blind create replay could double-create a remote issue;
3. on an ambiguous create completion (timeout / connection-loss) re-observes ONCE
   via the replay-safe ``observe`` seam and either RECOVERS (the create in fact
   landed) or retains the pending intent and DEFERS as ``commit_unknown`` so a
   later pass converges — it NEVER blind-replays a second create;
4. captures the returned Jira key on the typed :class:`CreateOutcome`; and
5. NEVER deletes a successfully-created remote issue — there is no delete seam in
   this module at all.

Its DECISION logic performs ZERO real I/O and reads no clock: the three injected
callables are the sole side-effect channels, so identical inputs yield equal
outputs. Frozen value types keep every outcome hashable and comparable.

Cross-sibling value types are loaded by file path via the package's shared
``lazy_load`` idiom (``_loader.py``), matching every other reconciler sibling.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
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

_policy = lazy_load("rebar_reconciler.failure_policy", "failure_policy.py")


# ── Injected-adapter signal value types ──────────────────────────────────────────

#: A "created" signal MUST carry a non-None key; the ambiguous statuses re-observe.
_AMBIGUOUS_CREATE_STATUSES: frozenset = frozenset({"timeout", "connection_lost"})


@dataclass(frozen=True, slots=True)
class CreateSignal:
    """What the injected ``create_execute`` adapter returns for the ONE physical create.

    ``status`` is one of ``created`` / ``timeout`` / ``connection_lost`` / ``permanent``.
    ``created`` carries the returned Jira ``known_key``; the two ambiguous statuses
    (``timeout`` / ``connection_lost``) trigger a single replay-safe re-observation;
    ``permanent`` is a terminal non-ambiguous failure that is NOT re-observed.
    """

    status: str
    known_key: str | None = None
    scope: object = FailureScope.ticket
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationSignal:
    """What the replay-safe ``observe`` seam returns after an ambiguous create.

    ``status`` is ``proven`` (the create demonstrably landed — carrying the observed
    ``known_key``) or ``inconclusive`` (identity could not be proven, so the pending
    intent is retained for a later convergence pass).
    """

    status: str
    known_key: str | None = None


# ── Result value type ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CreateOutcome:
    """The pure-decision result of coordinating one create.

    ``bucket`` is always the ``failure_policy`` projection of ``disposition``;
    ``known_key`` carries the Jira key whenever one is known (created or proven);
    ``create_call_count`` is 0 when the durable pending save failed and 1 otherwise
    (the create is issued at most once); ``pending_persisted`` records whether the
    intent was durably written.
    """

    identity: str
    disposition: object
    failure_scope: object
    bucket: str
    known_key: str | None
    create_call_count: int
    pending_persisted: bool
    diagnostics: tuple = field(default_factory=tuple)

    def has_known_key(self) -> bool:
        return self.known_key is not None


# ── Outcome constructors (bucket is always derived, never hardcoded) ──────────────


def _outcome(
    plan,
    disposition,
    scope,
    *,
    known_key=None,
    create_call_count,
    pending_persisted,
    diagnostics=(),
) -> CreateOutcome:
    return CreateOutcome(
        identity=plan.identity,
        disposition=disposition,
        failure_scope=scope,
        bucket=_policy.bucket_for(disposition),
        known_key=known_key,
        create_call_count=create_call_count,
        pending_persisted=pending_persisted,
        diagnostics=tuple(diagnostics),
    )


def _pending_save_failed(plan, exc) -> CreateOutcome:
    """AC1: the durable pending intent could not be written — permanent ticket-local
    failure with ZERO provider calls."""
    diag = getattr(exc, "args", None)
    message = str(diag[0]) if diag else exc.__class__.__name__
    return _outcome(
        plan,
        Disposition.permanent_failure,
        FailureScope.ticket,
        create_call_count=0,
        pending_persisted=False,
        diagnostics=({"stage": "persist_pending", "message": message},),
    )


def _created(plan, signal) -> CreateOutcome:
    """AC2/AC5: the single create landed — applied, carrying the returned key."""
    return _outcome(
        plan,
        Disposition.applied,
        FailureScope.none,
        known_key=signal.known_key,
        create_call_count=1,
        pending_persisted=True,
    )


def _permanent(plan, signal) -> CreateOutcome:
    """A non-ambiguous terminal create failure — not re-observed, no key."""
    diagnostics: tuple = ()
    if signal.diagnostic is not None:
        diagnostics = ({"stage": "create", "message": signal.diagnostic},)
    return _outcome(
        plan,
        Disposition.permanent_failure,
        signal.scope,
        create_call_count=1,
        pending_persisted=True,
        diagnostics=diagnostics,
    )


def _recovered(plan, observation) -> CreateOutcome:
    """AC3/AC5: an ambiguous create was PROVEN to have landed — recovered, carrying the
    observed key."""
    return _outcome(
        plan,
        Disposition.recovered,
        FailureScope.none,
        known_key=observation.known_key,
        create_call_count=1,
        pending_persisted=True,
    )


def _commit_unknown(plan, signal) -> CreateOutcome:
    """AC4: an ambiguous create whose observation is inconclusive — the pending intent
    is RETAINED (never unbound here) and the op defers as ``commit_unknown`` so a later
    pass converges."""
    return _outcome(
        plan,
        Disposition.commit_unknown,
        FailureScope.ticket,
        create_call_count=1,
        pending_persisted=True,
        diagnostics=({"stage": "create", "category": "ambiguous", "message": signal.status},),
    )


def _resolve_ambiguous(plan, signal, observe) -> CreateOutcome:
    """Handle a timeout / connection-loss completion by re-observing EXACTLY ONCE.

    Never issues a second create (blind-replay is forbidden). A proven observation
    recovers; an inconclusive one retains pending and defers."""
    observation = observe(plan)
    if observation.status == "proven":
        return _recovered(plan, observation)
    return _commit_unknown(plan, signal)


def _resolve_create(plan, signal, observe) -> CreateOutcome:
    """Project the single create signal onto a terminal outcome, re-observing only for
    the two ambiguous statuses."""
    if signal.status == "created":
        return _created(plan, signal)
    if signal.status in _AMBIGUOUS_CREATE_STATUSES:
        return _resolve_ambiguous(plan, signal, observe)
    return _permanent(plan, signal)


# ── Entry point ──────────────────────────────────────────────────────────────────


def coordinate_create(
    plan,
    *,
    persist_pending,
    create_execute,
    observe,
    budget_factory=None,
) -> CreateOutcome:
    """Durably persist the pending-binding intent, then issue exactly ONE create.

    ``persist_pending(plan)`` writes the intent and RAISES on failure (AC1);
    ``create_execute(plan) -> CreateSignal`` issues the SINGLE physical create;
    ``observe(plan) -> ObservationSignal`` is the replay-safe re-observation used ONLY
    after an ambiguous create. There is deliberately no delete/undo seam: a
    successfully-created remote issue is never deleted (AC6). ``budget_factory`` is
    accepted for parity with the sibling coordinator but a create is one-shot, so no
    transient retry budget is threaded here.
    """
    try:
        persist_pending(plan)
    except Exception as exc:  # noqa: BLE001 — any store-write failure aborts before create
        return _pending_save_failed(plan, exc)
    signal = create_execute(plan)
    return _resolve_create(plan, signal, observe)
