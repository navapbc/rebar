"""Provider-neutral pure-decision CREATE containment — the post-create key slice.

``contain_created(plan, known_key, *, record_key, attach_label, set_property, confirm)``
runs AFTER a create has returned a Jira key (REB-3115 S4 T2). It models the CANONICAL
write-ahead protocol proven by ``dispatch_one`` / ``binding_store`` (story 9622 / bug
387d): the returned key is contained onto the durable pending binding in a fixed order

1. ``record_key(plan, known_key)`` — persist the known key on the STILL-PENDING entry
   *before* any label, so a keyed-pending binding always exists for recovery;
2. ``attach_label(plan, known_key)`` — attach the canonical ``rebar-id`` label;
3. ``set_property(plan, known_key)`` — OPTIONAL entity-property enrichment; and
4. ``confirm(plan, known_key)`` — ``bind_confirm`` LAST, gating any downstream intent.

On ANY write failure after the create it NEVER deletes the remote issue (bug 387d):
the key is preserved on every abort so recovery can deterministically retro-attach the
remaining containment. The property step is best-effort — a failure there stays VISIBLE
(a diagnostic) and NEVER removes the already-attached label — but it still blocks the
final confirm so a later pass converges.

Its DECISION logic performs ZERO real I/O and reads no clock: the four injected
callables are the sole side-effect channels, so identical inputs yield equal outputs.
Each seam is RAISE-based — it returns None on success and raises on failure. Frozen
value types keep every outcome hashable and comparable.

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


# ── Result value type ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ContainmentOutcome:
    """The pure-decision result of containing a returned create key.

    ``bucket`` is always the ``failure_policy`` projection of ``disposition``;
    ``known_key`` is PRESERVED on every path (the create landed, so the key is always
    known); ``label_attached`` / ``property_attached`` / ``confirmed`` record how far
    the write-ahead sequence progressed. A caller gating a downstream lifecycle intent
    on ``confirmed`` holds it on every abort.
    """

    identity: str
    disposition: object
    failure_scope: object
    bucket: str
    known_key: str | None
    label_attached: bool
    property_attached: bool
    confirmed: bool
    diagnostics: tuple = field(default_factory=tuple)

    def has_known_key(self) -> bool:
        return self.known_key is not None


# ── Outcome constructor (bucket is always derived, never hardcoded) ───────────────


def _outcome(
    plan,
    disposition,
    scope,
    *,
    known_key,
    label_attached,
    property_attached,
    confirmed,
    diagnostics=(),
) -> ContainmentOutcome:
    return ContainmentOutcome(
        identity=plan.identity,
        disposition=disposition,
        failure_scope=scope,
        bucket=_policy.bucket_for(disposition),
        known_key=known_key,
        label_attached=label_attached,
        property_attached=property_attached,
        confirmed=confirmed,
        diagnostics=tuple(diagnostics),
    )


def _message(exc) -> str:
    args = getattr(exc, "args", None)
    return str(args[0]) if args else exc.__class__.__name__


def _aborted(
    plan,
    known_key,
    *,
    label_attached,
    property_attached,
    diagnostic,
) -> ContainmentOutcome:
    """A post-create write failed — safety_aborted with the key PRESERVED so recovery
    can retro-attach the remaining containment. NEVER confirmed, NEVER a delete."""
    return _outcome(
        plan,
        Disposition.safety_aborted,
        FailureScope.ticket,
        known_key=known_key,
        label_attached=label_attached,
        property_attached=property_attached,
        confirmed=False,
        diagnostics=(diagnostic,),
    )


def _contained(plan, known_key) -> ContainmentOutcome:
    """Every containment write landed — applied, confirmed, key preserved."""
    return _outcome(
        plan,
        Disposition.applied,
        FailureScope.none,
        known_key=known_key,
        label_attached=True,
        property_attached=True,
        confirmed=True,
    )


# ── Entry point ──────────────────────────────────────────────────────────────────


def contain_created(
    plan,
    known_key,
    *,
    record_key,
    attach_label,
    set_property,
    confirm,
) -> ContainmentOutcome:
    """Contain a returned create key onto the durable pending binding, in order.

    Each seam takes ``(plan, known_key)``, returns None on success, and RAISES on
    failure. The order is fixed — ``record_key`` (key persisted on the still-pending
    entry BEFORE any label) → ``attach_label`` (canonical ``rebar-id`` label) →
    ``set_property`` (optional enrichment) → ``confirm`` (``bind_confirm`` LAST). On
    ANY failure the key is PRESERVED and the issue is never deleted (bug 387d): a
    keyed-pending binding remains for deterministic keyed retro-attach by recovery.
    A ``set_property`` failure stays visible but never removes the already-attached
    label; it still blocks the final confirm. There is deliberately no
    delete/undo/rollback/unbind seam (AC5).
    """
    try:
        record_key(plan, known_key)
    except Exception as exc:  # noqa: BLE001 — any store-write failure aborts, key retained
        return _aborted(
            plan,
            known_key,
            label_attached=False,
            property_attached=False,
            diagnostic={"stage": "record_key", "message": _message(exc)},
        )
    try:
        attach_label(plan, known_key)
    except Exception as exc:  # noqa: BLE001 — key already recorded → keyed-pending recoverable
        return _aborted(
            plan,
            known_key,
            label_attached=False,
            property_attached=False,
            diagnostic={"stage": "attach_label", "message": _message(exc)},
        )
    try:
        set_property(plan, known_key)
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort; label is preserved
        return _aborted(
            plan,
            known_key,
            label_attached=True,
            property_attached=False,
            diagnostic={
                "stage": "set_property",
                "category": "enrichment",
                "message": _message(exc),
            },
        )
    try:
        confirm(plan, known_key)
    except Exception as exc:  # noqa: BLE001 — confirm gates downstream intents; leave keyed-pending
        return _aborted(
            plan,
            known_key,
            label_attached=True,
            property_attached=True,
            diagnostic={"stage": "confirm", "message": _message(exc)},
        )
    return _contained(plan, known_key)
