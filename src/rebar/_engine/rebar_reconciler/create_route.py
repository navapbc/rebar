"""Provider-neutral CREATE route selector + pure-decision full-create composition.

This is the S4 T3 cutover leaf (REB-3115). It gives outbound CREATE exactly ONE
route governed by ONE rollback selector (no dual-send), and composes the two proven
pure-decision slices — ``create_coordinator.coordinate_create`` (the durable
write-ahead + single physical create) and ``create_containment.contain_created``
(the post-create key containment) — into a single terminal decision that ALSO
derives the create-before-link / parent-before-child gating verdict.

Three things live here:

1. :func:`create_route` — the SINGLE rollback toggle, mirroring
   ``reconcile_helpers._write_facade_enabled``: default ON (coordinator), a falsey
   ``REBAR_RECONCILER_CREATE_ROUTE`` rolls back to the legacy value. A caller
   consults it ONCE and dispatches EXACTLY ONE path.
2. :func:`coordinate_full_create` — the pure-decision composition. It performs ZERO
   real I/O and reads no clock: the eight injected callables are the sole
   side-effect channels, so identical inputs yield equal outputs. It NEVER deletes a
   created remote issue (the composed slices have no delete seam).
3. :func:`run_coordinated_outbound_create` — the ONLY impure helper here: it wires
   the eight seams over a live ``client`` / ``binding_store`` and runs the
   composition, so the outbound leaf's diff stays a one-line delegation and neither
   ``dispatch_one.py`` nor ``applier.py`` grows toward its 800-LOC cap.

Cross-sibling value types are loaded by file path via the package's shared
``lazy_load`` idiom (``_loader.py``), matching every other reconciler sibling.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._backend import TicketTransport

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

_coordinator_mod = lazy_load("rebar_reconciler.create_coordinator", "create_coordinator.py")
_containment_mod = lazy_load("rebar_reconciler.create_containment", "create_containment.py")
_outcome_mod = lazy_load("rebar_reconciler.operation_outcome", "operation_outcome.py")
_policy = lazy_load("rebar_reconciler.failure_policy", "failure_policy.py")

coordinate_create = _coordinator_mod.coordinate_create
CreateSignal = _coordinator_mod.CreateSignal
ObservationSignal = _coordinator_mod.ObservationSignal
contain_created = _containment_mod.contain_created
Disposition = _outcome_mod.Disposition
FailureScope = _outcome_mod.FailureScope


# ── The single rollback selector (AC6 — mirrors ``_write_facade_enabled``) ────────

COORDINATOR_ROUTE = "coordinator"
LEGACY_ROUTE = "legacy"

_LEGACY_TOKENS: frozenset = frozenset({"legacy", "0", "false", "off", "no"})
_COORDINATOR_TOKENS: frozenset = frozenset({"coordinator", "1", "true", "on", "yes"})


def create_route() -> str:
    """Select EXACTLY ONE create route (never dual-send).

    Default (unset ``REBAR_RECONCILER_CREATE_ROUTE``) is the coordinated write-ahead
    path (:data:`COORDINATOR_ROUTE`). A falsey value rolls back to the legacy
    create+delete-rollback value (:data:`LEGACY_ROUTE`); an unrecognized value raises
    ``ValueError`` so a typo cannot silently mis-route.
    """
    raw = os.environ.get("REBAR_RECONCILER_CREATE_ROUTE")  # read-via: rollback-toggle
    if raw is None:
        return COORDINATOR_ROUTE  # default: coordinated write-ahead path
    v = raw.strip().lower()
    if v in _LEGACY_TOKENS:
        return LEGACY_ROUTE
    if v in _COORDINATOR_TOKENS:
        return COORDINATOR_ROUTE
    raise ValueError(f"invalid REBAR_RECONCILER_CREATE_ROUTE: {raw!r}")


# ── Pure-decision full-create composition ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CoordinatedCreateOutcome:
    """The pure-decision result of composing create + containment.

    ``bucket`` is always the ``failure_policy`` projection of ``disposition``;
    ``dependents_released`` is the create-before-link / parent-before-child gate: it
    is True ONLY when both the create AND its confirmation are proven, so a link/child
    mutation gated on it is HELD on every non-confirmed path (AC5).
    """

    identity: str
    disposition: object
    failure_scope: object
    bucket: str
    known_key: str | None
    create_call_count: int
    pending_persisted: bool
    label_attached: bool
    property_attached: bool
    confirmed: bool
    dependents_released: bool
    diagnostics: tuple = field(default_factory=tuple)

    def has_known_key(self) -> bool:
        return self.known_key is not None


def _held_before_landing(plan, create) -> CoordinatedCreateOutcome:
    """The create did not demonstrably land (permanent_failure / commit_unknown) — no
    containment is attempted and dependents are HELD (AC5)."""
    return CoordinatedCreateOutcome(
        identity=plan.identity,
        disposition=create.disposition,
        failure_scope=create.failure_scope,
        bucket=create.bucket,
        known_key=create.known_key,
        create_call_count=create.create_call_count,
        pending_persisted=create.pending_persisted,
        label_attached=False,
        property_attached=False,
        confirmed=False,
        dependents_released=False,
        diagnostics=tuple(create.diagnostics),
    )


def _combine(plan, create, contain) -> CoordinatedCreateOutcome:
    """Combine a landed create with its containment outcome, deriving the gate."""
    if contain.confirmed:
        disposition = create.disposition
        failure_scope = FailureScope.none
        dependents_released = True
    else:
        disposition = contain.disposition
        failure_scope = contain.failure_scope
        dependents_released = False
    return CoordinatedCreateOutcome(
        identity=plan.identity,
        disposition=disposition,
        failure_scope=failure_scope,
        bucket=_policy.bucket_for(disposition),
        known_key=create.known_key,
        create_call_count=create.create_call_count,
        pending_persisted=create.pending_persisted,
        label_attached=contain.label_attached,
        property_attached=contain.property_attached,
        confirmed=contain.confirmed,
        dependents_released=dependents_released,
        diagnostics=tuple(create.diagnostics) + tuple(contain.diagnostics),
    )


def coordinate_full_create(
    plan,
    *,
    persist_pending,
    create_execute,
    observe,
    record_key,
    attach_label,
    set_property,
    confirm,
) -> CoordinatedCreateOutcome:
    """Compose ``coordinate_create`` then ``contain_created`` into ONE terminal decision.

    If the create does not demonstrably land (permanent_failure or commit_unknown —
    no known key), containment is skipped and dependents are HELD. If it lands
    (applied or recovered — a key is known), the returned key is contained; the
    combined disposition is the create's when containment confirms and the
    containment's (safety_aborted) otherwise, and dependents are released ONLY on a
    confirmed containment (AC5). The composition has no delete seam.
    """
    create = coordinate_create(
        plan,
        persist_pending=persist_pending,
        create_execute=create_execute,
        observe=observe,
    )
    if not create.has_known_key():
        return _held_before_landing(plan, create)
    contain = contain_created(
        plan,
        create.known_key,
        record_key=record_key,
        attach_label=attach_label,
        set_property=set_property,
        confirm=confirm,
    )
    return _combine(plan, create, contain)


def should_hold_dependent(outcome) -> bool:
    """The create-before-link / parent-before-child gate predicate.

    A link/child mutation whose create dependency's :class:`CoordinatedCreateOutcome`
    is not ``dependents_released`` MUST be held (deferred), not dispatched.
    """
    return not outcome.dependents_released


# ── Live wiring helper (the only impure surface here) ─────────────────────────────


class _CreatePlan:
    """A minimal create plan carrying the ``.identity`` the pure slices read."""

    __slots__ = ("identity",)

    def __init__(self, identity: str) -> None:
        self.identity = identity


class _CreateCapture:
    """The live-seam side channels the pure composition cannot carry.

    ``coordinate_full_create`` swallows every seam exception into a diagnostic string
    and returns a value object, which is exactly right for the pure decision but loses
    the two things a create core adapting back to ``create_one``'s contract needs: the
    raw create-response dict (for the returned dict + the immutable id) and the ORIGINAL
    exception objects. The batch core re-raises the EXACT object — preserving
    ``HTTPError.code`` so ``handle_create``'s 404-soft-fail vs non-404-fail-fast taxonomy
    stays byte-identical — instead of a collapsed generic error.
    """

    __slots__ = ("containment_error", "create_error", "persist_error", "result")

    def __init__(self) -> None:
        self.result: dict = {}
        self.create_error: BaseException | None = None
        self.persist_error: BaseException | None = None
        self.containment_error: BaseException | None = None


class _CoordinatedSeams:
    """The eight live create seams as bound methods over one client/binding_store.

    Holding the seams on an object (rather than as closures inside
    ``_run_coordinated_create``) keeps each seam's branching in its OWN function, so the
    wiring entry point stays well under its McCabe ceiling. Every write seam captures the
    original exception object into ``capture`` and re-raises, so the batch core can hand
    back the EXACT type (esp. ``HTTPError``). The composition is otherwise identical to
    ``create_one``'s write-ahead protocol and NEVER deletes on a post-create failure.
    """

    def __init__(
        self, local_id, ticket_data, *, client: TicketTransport, binding_store, capture
    ) -> None:
        self.local_id = local_id
        self.ticket_data = ticket_data
        self.client = client
        self.binding_store = binding_store
        self.capture = capture
        self._errors = lazy_load("rebar_reconciler._errors", "_errors.py")
        self._call = lazy_load(
            "rebar_reconciler.batch_dispatch", "batch_dispatch.py"
        )._call_with_retry
        self._persist_error_type = lazy_load(
            "rebar_reconciler.binding_store", "binding_store.py"
        ).BindingPersistError

    def persist_pending(self, _plan):
        if self.binding_store is not None and self.local_id:
            try:
                self.binding_store.bind_pending(self.local_id)
                self.binding_store.save()
            except Exception as exc:
                err = self._persist_error_type(
                    f"write-ahead bind_pending persist failed for {self.local_id!r}; "
                    f"create skipped: {exc!r}"
                )
                self.capture.persist_error = err
                raise err from exc

    def create_execute(self, _plan):
        try:
            result = self.client.create_issue(self.ticket_data)
        except (TimeoutError, self._errors.RetryExhaustedError) as exc:  # ambiguous — re-observe
            return CreateSignal(status="timeout", diagnostic=str(exc))
        except ConnectionError as exc:  # ambiguous — re-observe
            return CreateSignal(status="connection_lost", diagnostic=str(exc))
        except Exception as exc:  # noqa: BLE001 — terminal create failure, captured for re-raise
            self.capture.create_error = exc
            return CreateSignal(status="permanent", diagnostic=str(exc))
        self.capture.result = result if isinstance(result, dict) else {}
        key = self.capture.result.get("key", "")
        if key:
            return CreateSignal(status="created", known_key=key)
        return CreateSignal(status="permanent", diagnostic="create returned no key")

    def observe(self, _plan):
        hits = self.client.search_issues(f'labels = "rebar-id:{self.local_id}"')
        if hits:
            return ObservationSignal(status="proven", known_key=hits[0].get("key", ""))
        return ObservationSignal(status="inconclusive")

    def record_key(self, _plan, known_key):
        if self.binding_store is not None and self.local_id:
            try:
                self.binding_store.record_pending_key(self.local_id, known_key)
                _record_id = getattr(self.binding_store, "record_jira_id", None)
                if _record_id is not None:
                    _record_id(self.local_id, self.capture.result.get("id", ""))
                self.binding_store.save()
            except Exception as exc:
                err = self._persist_error_type(
                    f"write-ahead record_pending_key persist failed for "
                    f"{self.local_id!r} (key {known_key!r}): {exc!r}"
                )
                self.capture.containment_error = err
                raise err from exc

    def attach_label(self, _plan, known_key):
        try:
            self._call(self.client.add_label, known_key, f"rebar-id:{self.local_id}")
        except Exception as exc:
            self.capture.containment_error = exc
            raise

    def set_property(self, _plan, known_key):
        try:
            self._call(self.client.set_entity_property, known_key, "local_id", self.local_id)
        except Exception as exc:
            self.capture.containment_error = exc
            raise

    def confirm(self, _plan, known_key):
        if self.binding_store is not None and self.local_id:
            try:
                self.binding_store.bind_confirm(self.local_id, known_key)
            except Exception as exc:
                self.capture.containment_error = exc
                raise


def _run_coordinated_create(local_id, ticket_data, *, client: TicketTransport, binding_store):
    """Wire the eight seams over a live client/binding_store; return (outcome, capture).

    Mirrors ``dispatch_one.create_one``'s write-ahead protocol (bind_pending+save →
    create → record_pending_key(+record_jira_id)+save → add_label → set_entity_property
    → bind_confirm) and NEVER deletes on any post-create failure (bug 387d). ``ticket_data``
    is the payload handed to ``create_issue`` (the batch path passes the ALREADY-translated
    fields; the typed leaf passes ``mutation.payload``). The :class:`_CreateCapture`
    retains the create response and the original create/persist/containment exceptions so
    a caller can re-raise the exact object.
    """
    capture = _CreateCapture()
    seams = _CoordinatedSeams(
        local_id, ticket_data, client=client, binding_store=binding_store, capture=capture
    )
    outcome = coordinate_full_create(
        _CreatePlan(local_id),
        persist_pending=seams.persist_pending,
        create_execute=seams.create_execute,
        observe=seams.observe,
        record_key=seams.record_key,
        attach_label=seams.attach_label,
        set_property=seams.set_property,
        confirm=seams.confirm,
    )
    return outcome, capture


def run_coordinated_outbound_create(mutation, *, client: TicketTransport, binding_store):
    """Wire the eight seams over a live client/binding_store and run the composition.

    The typed-dispatch facade (``apply_outbound._apply_outbound_create``): mirrors
    ``dispatch_one.create_one``'s write-ahead protocol and NEVER deletes on any
    post-create failure (bug 387d). Returns the terminal
    :class:`CoordinatedCreateOutcome`. Shares the single ``_run_coordinated_create``
    wiring with the batch core so typed and batch creates cannot drift.
    """
    payload = dict(mutation.payload or {})
    local_id = payload.get("local_id") or getattr(mutation, "target", "") or ""
    outcome, _capture = _run_coordinated_create(
        local_id, payload, client=client, binding_store=binding_store
    )
    return outcome


def _coordinated_create_core(
    local_id,
    ticket_data,
    *,
    client: TicketTransport,
    binding_store=None,
    repo_root=None,
    deferred_creates=None,
    mutation=None,
) -> dict | None:
    """The coordinated create->contain MIDDLE core, adapted to ``create_one``'s contract.

    Injected as ``create_one(..., create_core=...)`` by ``apply_handlers.handle_create``
    when the ``create_route`` selector is the coordinator. Runs the same composition as
    :func:`run_coordinated_outbound_create` over the ALREADY-translated ``ticket_data``,
    then maps the terminal :class:`CoordinatedCreateOutcome` back to what ``create_one``
    expects from a create core:

    * **confirmed** → return the raw create-response dict (with ``"key"``) so the postlude
      runs and ``handle_create`` counts the REST call and sets ``outcome["result"]``;
    * **landed-but-NOT-confirmed** (containment safety_aborted, key known) → emit the SAME
      ``_emit_create_identity_alert`` (parity, no delete) and RE-RAISE the original
      containment exception, so no key reaches the postlude;
    * **persist failure** (bind_pending save failed) → RAISE ``BindingPersistError``;
    * **permanent create failure** → RE-RAISE the ORIGINAL create exception object,
      preserving the exact type (esp. ``HTTPError``) so ``handle_create``'s 404 taxonomy
      is byte-identical (a keyless create response — no exception — returns the dict,
      matching legacy);
    * **commit_unknown** (ambiguous create, re-observe inconclusive, no key) → DEFER:
      append ``mutation`` to ``deferred_creates`` and return ``None`` so it is retried next
      pass WITHOUT a hard error and WITHOUT a false "applied" — never blind-replaying a
      second create.
    """
    outcome, capture = _run_coordinated_create(
        local_id, ticket_data, client=client, binding_store=binding_store
    )
    if capture.persist_error is not None:
        raise capture.persist_error
    if outcome.confirmed:
        return capture.result
    if outcome.has_known_key():
        dispatch_one = lazy_load("rebar_reconciler.dispatch_one", "dispatch_one.py")
        dispatch_one._emit_create_identity_alert(local_id, outcome.known_key, repo_root)
        raise capture.containment_error or RuntimeError(
            f"coordinated create for {local_id!r} landed at {outcome.known_key!r} "
            "but was not confirmed and captured no containment error"
        )
    if outcome.disposition == Disposition.commit_unknown:
        if deferred_creates is not None and mutation is not None:
            deferred_creates.append(mutation)
        return None
    if capture.create_error is not None:
        raise capture.create_error
    return capture.result
