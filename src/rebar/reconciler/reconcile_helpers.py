#!/usr/bin/env python3
"""Pass helpers for no-write plans, differ filtering, runtime binding, and baselines.

The reconciliation spine calls these canonical owners directly; this module
has no reverse import of ``reconcile``. Sibling loading stays patchable.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _load(_name: str, relpath: str):
    """Import a sibling through the patchable package loader seam."""
    return importlib.import_module(
        "rebar.reconciler." + relpath.removesuffix(".py").replace("/", ".")
    )


def _build_plan_entries(mutations) -> list[dict]:
    """Build a list of per-mutation plan entries for the no-write report.

    Each entry carries enough detail to be a useful plan:
    ``{direction, action, target, local_id}``. Tolerates both typed Mutation
    objects (``.direction``/``.action`` enums) and legacy dict mutations.
    """
    entries: list[dict] = []
    for m in mutations:
        direction = getattr(m, "direction", None)
        action = getattr(m, "action", None)
        if direction is not None or action is not None:
            d = str(getattr(direction, "value", direction) or "")
            a = str(getattr(action, "value", action) or "")
            target = getattr(m, "target", None)
            prov = getattr(m, "provenance", None) or {}
            local_id = prov.get("local_id") if isinstance(prov, Mapping) else None
        else:
            d = str(m.get("direction", "") or "")
            a = str(m.get("action", "") or "")
            target = m.get("key") or m.get("target")
            local_id = m.get("local_id")
        entries.append(
            {
                "direction": d,
                "action": a,
                "target": target,
                "local_id": local_id,
            }
        )
    return entries


class _NoOpSyncLogger:
    """No-op stand-in for SyncLogger used by cap-0 (no-write) passes.

    Implements the full surface ``reconcile_once`` calls on a sync logger
    (``log`` and ``close``) but writes nothing — so a dry-run/preview
    pass produces no ``sync-log-<pass>.jsonl`` file.
    """

    def log(self, *_args, **_kwargs) -> None:
        return None

    def close(self) -> None:
        return None


# These emissions require real local state. Match source and reason together
# so invariant seed mutations sharing a reason remain intact.
_SNAPSHOT_DIFFER_LOCAL_STATE_EMISSIONS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("outbound", "update", "field_drift"),
        ("outbound", "create", "unbound_local"),
    }
)


def drop_snapshot_differ_local_state_emissions(mutations: list[Any]) -> list[Any]:
    """Drop 2 outbound emissions invalid when both differ inputs are remote snapshots.

    ``run_differs`` passes previous/current Jira fetches to a differ expecting
    local/Jira state. Its ``field_drift`` would replay stale fields and
    ``unbound_local`` could resurrect an issue merely absent from search.
    Preserve inbound ``jira_new``, invariant seeds, and all other emissions by
    matching source as well as reason. Legacy dicts pass through; return a new
    list. Remove this suppression if the caller starts passing real local state.
    """
    kept: list[Any] = []
    for mutation in mutations:
        provenance = getattr(mutation, "provenance", None)
        if not isinstance(provenance, Mapping):
            kept.append(mutation)
            continue
        signature = (
            str(getattr(getattr(mutation, "direction", None), "value", "")),
            str(getattr(getattr(mutation, "action", None), "value", "")),
            str(provenance.get("reason") or ""),
        )
        if provenance.get("source") == "differ" and signature in (
            _SNAPSHOT_DIFFER_LOCAL_STATE_EMISSIONS
        ):
            continue
        kept.append(mutation)
    return kept


def drop_snapshot_differ_property_bound_emissions(
    mutations: list[Any], curr_snapshot: Mapping[str, Any]
) -> list[Any]:
    """Drop snapshot-differ creates for remotes carrying a ``local_id`` property.

    At ``run_differs`` the generic differ still compares previous-vs-current Jira
    snapshots, not local-vs-Jira state. A current remote with a ``local_id``
    entity property is managed identity, even if its label/binding are missing,
    so the legacy edge trigger must not classify it as Jira-native.
    """
    kept: list[Any] = []
    for mutation in mutations:
        provenance = getattr(mutation, "provenance", None)
        target = getattr(mutation, "target", None)
        fields = curr_snapshot.get(target) if target is not None else None
        property_bound = isinstance(fields, Mapping) and bool(fields.get("local_id"))
        if (
            property_bound
            and isinstance(provenance, Mapping)
            and provenance.get("source") == "differ"
            and str(getattr(getattr(mutation, "direction", None), "value", "")) == "inbound"
            and str(getattr(getattr(mutation, "action", None), "value", ""))
            in {"create", "conflict"}
        ):
            continue
        kept.append(mutation)
    return kept


def _accepts_synced_fields_out(fn: Any) -> bool:
    """Pass ``synced_fields_out`` only to signatures that accept it.

    Unknown signatures use the fetch-only baseline fallback instead of risking
    a pass-breaking ``TypeError``.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "synced_fields_out" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _accepts_client(fn: Any) -> bool:
    """Forward captured transport only when the apply signature accepts ``client``.

    Unknown or narrow signatures retain the applier's ambient fallback.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "client" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _accepts_ticket_plans(fn: Any) -> bool:
    """Whether ``fn`` accepts ``ticket_plans`` (RP-03 S2 T3) — mirrors ``_accepts_client``."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "ticket_plans" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _resolve_pass_transport(ctx: Any):
    """Honor the applier's patchable transport seam.

    Missing seams return ``None``; load failures raise for writes and fall back
    for no-write passes.
    """
    loader = getattr(ctx.applier, "_load_acli", None)
    if not callable(loader):
        return None
    try:
        return loader()
    except Exception:  # a no-write pass tolerates absent scope; a write pass re-raises below
        if ctx.persist:
            raise
        return None


def bind_operation_runtime(ctx: Any, compose: Any) -> None:
    """Capture one backend and transport for the pass's apply phase.

    The applier's loader remains patchable for tests. Composition failures
    raise for writing passes but leave no-write passes on their ambient path.
    """
    try:
        transport = _resolve_pass_transport(ctx)
        runtime = compose(repo_root=ctx.repo_root)
        backend = runtime.build_backend(transport=transport)
    except Exception:  # no-write pass tolerates absent scope; a write pass re-raises below
        if ctx.persist:
            raise
        return
    ctx.runtime_backend = backend
    ctx.runtime_transport = getattr(backend, "transport", None)


def _advance_baselines(
    binding_store: Any,
    curr_snapshot: Mapping[str, Any],
    synced_fields: Mapping[str, Mapping[str, Any]] | None = None,
) -> int:
    """Advance confirmed bindings from fetched and then confirmed written fields.

    The pass-start snapshot predates outbound apply. Overlay only landed
    per-mutation writes, including out-of-window keys, so the baseline records
    last-synced rather than stale fetched values. Unconfirmed bindings and
    absent fetch values do not advance from the snapshot.
    """
    advanced = 0
    synced = synced_fields or {}
    overlaid = 0
    for local_id, entry in binding_store.all_bindings().items():
        if entry.get("state") != "confirmed":
            continue
        jira_key = entry.get("jira_key")
        if jira_key and jira_key in curr_snapshot:
            binding_store.set_baseline(local_id, curr_snapshot[jira_key])
            _advance_peer_parent(binding_store, local_id, curr_snapshot[jira_key])
            advanced += 1
        pushed = synced.get(local_id)
        if pushed:
            # getattr-guarded exactly as _advance_peer_parent guards set_peer_parent: a
            # store predating this method (or an older test double) must degrade to the
            # fetch-only advance, not raise mid-pass.
            merge = getattr(binding_store, "merge_baseline", None)
            if merge is not None:
                merge(local_id, dict(pushed))
                overlaid += 1
    if synced:
        # Observability for the DELIBERATE non-advance: a soft-failed mutation contributes
        # nothing to `synced`, so comparing this against the pass's outbound_update lines
        # separates "the write failed" from "there was nothing to push" (bug e6e9).
        print(  # noqa: T201
            f"RECON: baseline_overlay bindings={overlaid} pushed_bindings={len(synced)}",
            file=sys.stderr,
        )
    return advanced


def _advance_peer_parent(binding_store: Any, local_id: str, entry: Mapping[str, Any]) -> None:
    """Record a peer parent only when the snapshot carries the parent key.

    Explicit ``None`` means observed empty; an absent key leaves earlier
    evidence intact. Older stores without the setter remain a no-op.
    """
    if "parent" not in entry:
        return
    setter = getattr(binding_store, "set_peer_parent", None)
    if setter is None:
        return
    parent = entry.get("parent")
    key = parent.get("key") if isinstance(parent, dict) else None
    setter(local_id, key if isinstance(key, str) and key else None)


def _write_prev_snapshot_key_set(prev_path: Path, curr_snapshot: Mapping[str, Any]) -> None:
    """Persist only Jira-key membership for the next pass's edge detection.

    Moved from reconcile.py (ticket 0fa2) to keep that module under the 800-LOC cap;
    this module is now the canonical owner.
    """
    key_set: dict[str, dict[str, Any]] = {jira_key: {} for jira_key in sorted(curr_snapshot)}
    prev_path.write_text(json.dumps(key_set, separators=(",", ":")) + "\n")
