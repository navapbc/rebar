#!/usr/bin/env python3
"""Typed-mutation dispatch: the (direction, action) routing table + its dispatcher.

The single cohesive registry the typed-apply path and reconcile.py's dispatch
table consume. _build_leaves binds each valid (direction, action) pair to its leaf
handler (imported from apply_outbound/apply_inbound) and checks that the bound
pairs are exactly mutation._VALID_COMBINATIONS; _LEAF_NAMES is the parallel name
map _apply_typed uses to derive a leaf_name for the rebar-id audit; _apply_typed
looks a mutation up in the table, runs the rebar-id write guard, and invokes the
handler. applier re-exports all.

Imports downward only (apply_base + the leaf modules); never imports applier.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._backend import TicketTransport

from rebar.reconciler.apply_base import (
    ApplyResult,
    _load_errors_module,
    _load_mutation_module,
)
from rebar.reconciler.apply_inbound import (
    _apply_inbound_clean_label,
    _apply_inbound_conflict,
    _apply_inbound_create,
    _apply_inbound_repair_property,
    _apply_inbound_update,
)
from rebar.reconciler.apply_outbound import (
    _apply_outbound_conflict,
    _apply_outbound_create,
    _apply_outbound_delete,
    _apply_outbound_probe,
    _apply_outbound_update,
)
from rebar.reconciler.rebar_id_audit import _audit_rebar_id_label_writes


def _build_leaves() -> dict[tuple[Any, Any], Callable[..., ApplyResult]]:
    """Build the _LEAVES registry.

    Built eagerly at module import by binding the leaf handler for each pair.
    The bound pairs must be exactly mutation._VALID_COMBINATIONS, so a pair the
    allowlist admits always has a handler and an invalid pair (e.g. outbound +
    clean_label) never does.
    """
    mut_mod = _load_mutation_module()
    D = mut_mod.MutationDirection
    A = mut_mod.MutationAction
    handlers: dict[tuple[Any, Any], Callable[..., ApplyResult]] = {
        (D.outbound, A.create): _apply_outbound_create,
        (D.outbound, A.update): _apply_outbound_update,
        (D.outbound, A.delete): _apply_outbound_delete,
        (D.outbound, A.probe): _apply_outbound_probe,
        (D.outbound, A.conflict): _apply_outbound_conflict,
        (D.inbound, A.create): _apply_inbound_create,
        (D.inbound, A.update): _apply_inbound_update,
        (D.inbound, A.clean_label): _apply_inbound_clean_label,
        (D.inbound, A.repair_property): _apply_inbound_repair_property,
        (D.inbound, A.conflict): _apply_inbound_conflict,
    }
    valid = mut_mod._VALID_COMBINATIONS
    if set(handlers) != valid:
        raise RuntimeError(
            "typed_dispatch leaves do not match mutation._VALID_COMBINATIONS: "
            f"unhandled={sorted(map(str, valid - set(handlers)))} "
            f"unlisted={sorted(map(str, set(handlers) - valid))}"
        )
    return handlers


# The dispatch registry. Keys are (MutationDirection, MutationAction) tuples;
# values are leaf handler callables of shape (mutation, *, client=None) -> ApplyResult.
_LEAVES: dict[tuple[Any, Any], Callable[..., ApplyResult]] = _build_leaves()


# Mapping from (MutationDirection.value, MutationAction.value) → canonical leaf name.
# Mirrors the _LEAVES dispatch table; used by _apply_typed to derive leaf_name for
# the audit without needing to inspect function names.
_LEAF_NAMES: dict[tuple[str, str], str] = {
    ("outbound", "create"): "outbound_create",
    ("outbound", "update"): "outbound_update",
    ("outbound", "delete"): "outbound_delete",
    ("outbound", "probe"): "outbound_probe",
    ("outbound", "conflict"): "outbound_conflict",
    ("inbound", "create"): "inbound_create",
    ("inbound", "update"): "inbound_update",
    ("inbound", "clean_label"): "inbound_clean_label",
    ("inbound", "repair_property"): "inbound_repair_property",
    ("inbound", "conflict"): "inbound_conflict",
}


def _apply_typed(
    mutation, *, client: TicketTransport | None = None, repo_root=None, binding_store=None
) -> ApplyResult:
    """Typed-mutation dispatch via _LEAVES.

    Looks up (mutation.direction, mutation.action) in _LEAVES and invokes the
    handler. Raises UnknownActionError with zero side-effects (no client calls,
    no I/O) if the pair is not registered.

    Calls _audit_rebar_id_label_writes BEFORE invoking the leaf so that any
    unauthorized rebar-id label mutation is blocked prior to side-effects.
    """
    key = (mutation.direction, mutation.action)
    handler = _LEAVES.get(key)
    if handler is None:
        errs = _load_errors_module()
        raise errs.UnknownActionError(
            f"unknown (direction={mutation.direction.value!s}, action={mutation.action.value!s})"
        )
    # Audit: derive leaf_name from the (direction, action) pair and run the
    # rebar-id label write guard before any leaf side-effect occurs.
    leaf_name = _LEAF_NAMES.get((mutation.direction.value, mutation.action.value), "")
    _audit_rebar_id_label_writes(leaf_name, [mutation])
    # All inbound leaves accept repo_root; outbound leaves now do too. Pass it
    # uniformly so the leaves can write to the local tracker when applicable.
    # Inspect the handler signature once to decide whether to pass repo_root,
    # rather than catching a broad TypeError (which would silently swallow
    # genuine TypeErrors raised from inside the leaf body — bug surfaced in
    # PR #375 review).
    import inspect as _inspect

    try:
        sig = _inspect.signature(handler)
        _has_var_kw = any(p.kind is _inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        accepts_repo_root = "repo_root" in sig.parameters or _has_var_kw
        accepts_binding_store = "binding_store" in sig.parameters or _has_var_kw
    except (TypeError, ValueError):
        # Builtins / C-extensions don't expose signatures: fall back to passing
        # repo_root (legacy behaviour) but NOT binding_store (only leaves that
        # explicitly declare it consume it — ticket 1577).
        accepts_repo_root = True
        accepts_binding_store = False

    _leaf_kwargs: dict[str, Any] = {"client": client}
    if accepts_repo_root:
        _leaf_kwargs["repo_root"] = repo_root
    if accepts_binding_store:
        _leaf_kwargs["binding_store"] = binding_store
    return handler(mutation, **_leaf_kwargs)
