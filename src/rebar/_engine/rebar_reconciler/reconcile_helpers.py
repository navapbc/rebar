#!/usr/bin/env python3
"""reconcile_helpers.py — pass-support utilities extracted from reconcile.py.

These are the leaf helpers that a reconcile pass leans on but which carry no
back-edge to the ``reconcile_once`` spine: the no-write plan renderer, the
``_NoOpSyncLogger`` cap-0 stand-in, the snapshot-differ local-state emission
filter, the RP-04 S3 runtime-binding cluster, and the ADR-0026 baseline
advance. The status-preflight scan, the binding-store commit-back, the
ticket-CLI reader, and the selection/filter-scope cluster moved out to the
sibling ``pass_support.py`` (ticket piscine-bullish-cowbird, module-size
headroom). ``reconcile.py`` calls these canonical owners directly instead of
re-exporting their private names.

Loader convention: like every sibling in this package (and mirrored by reconcile.py /
run_differs.py), this module loads its own siblings (``config.py``, ``alert_store.py``) by file
path via the local ``_load`` helper (``importlib.util.spec_from_file_location``), so it resolves
both under the real package and when a single module is loaded standalone in tests. It imports
NOTHING from reconcile.py; callers use this module as the canonical owner for
these private helpers.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# ``lazy_load`` centralizes the by-path sibling-loader idiom (rebar_reconciler/
# _loader.py). Import it normally when package context exists, else bootstrap it
# by file path — this module is itself exec'd standalone via
# spec_from_file_location in tests.
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


def _load(name: str, relpath: str):
    """Load a sibling module by relative file path, registering it in sys.modules.

    Returns the cached module when ``name`` is already in ``sys.modules``;
    this allows test fixtures to pre-register patched modules and have
    ``reconcile_once`` reuse them rather than loading fresh copies. Delegates to
    the shared ``lazy_load`` helper (the package-wide by-path loader).
    """
    return lazy_load(name, relpath)


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


# The differ emissions that are only sound when ``local_state`` really is local state:
# ``(outbound, update)``/``field_drift`` (bug 727f) and ``(outbound, create)``/
# ``unbound_local`` (bug d103-c3f8-2fbc-4c97). Keyed on BOTH ``source`` and ``reason`` so
# an invariant SEED mutation that happens to reuse a reason string is never swept up.
_SNAPSHOT_DIFFER_LOCAL_STATE_EMISSIONS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("outbound", "update", "field_drift"),
        ("outbound", "create", "unbound_local"),
    }
)


def drop_snapshot_differ_local_state_emissions(mutations: list[Any]) -> list[Any]:
    """Discard the differ emissions that presume a real ``local_state`` argument.

    Bugs 727f-b351-59ba-4b3b and d103-c3f8-2fbc-4c97 — one cause, two symptoms.

    ``differ.compute_mutations`` documents its arguments as ``(local_state, jira_state)``
    — the LOCAL source of truth against the Jira working set — and says so precisely
    because that contract REPLACED the legacy ``(prev_snapshot, next_snapshot)`` one.
    ``run_differs`` was never migrated: it still passes the legacy pair, both halves of
    which are REMOTE Jira state (``prev_snapshot`` is a persisted earlier fetch,
    ``curr_snapshot`` a fresh one). At that one call site ``local_state`` is therefore not
    local state at all, and the differ's two local-state-dependent arms misfire:

    * **``field_drift``** (key in both) — ``_compute_mutations_emit_both`` reads every
      prev->curr REMOTE field change as local-wins drift and emits an
      ``(outbound, update)`` carrying the STALE prev value. It never converges:
      ``reconcile.py`` advances the prev snapshot from the PRE-APPLY fetch, so an outbound
      write applied during pass N is invisible to ``prev`` at pass N+1 — a fully converged
      pair is re-planned as outbound work, and a read-only pass (which never advances
      ``prev``) re-plans the same phantom forever. Applying it changes nothing either: the
      payload is a bare field dict, not ``{"changed_fields": ...}``, so
      ``batch_dispatch._mutation_to_batch_dict`` resolves its fields to ``{}``. It is
      unsatisfiable while still spending the per-mode mutation cap and inflating
      ``mutation_count``.
    * **``unbound_local``** (key in ``local_state`` only) —
      ``_compute_mutations_emit_local_only`` emits an ``(outbound, create)`` for a key
      that is really just "present in the previous fetch, absent from this one". That is
      indistinguishable from a key that merely aged out of the working-set query, and the
      create RESURRECTS the issue from stale prev fields. It also violates ADR 0028
      (``docs/adr/0028-reconciler-bound-but-absent-not-deleted.md``, Decision para 1): no
      destructive or terminal action may be driven by a key's absence from the fetched
      snapshot.

    The differ's third arm — key in ``jira_state`` only, ``(inbound, create)`` with reason
    ``jira_new`` — IS correct here: at this call site it means a genuinely new remote
    issue, which is the snapshot diff's one real job. It is preserved, as is every other
    differ emission (inbound conflict/probe, ``dangling_jira_local_id``,
    ``duplicate_local_id``, ``ambiguous_local_binding``, ``repair_property`` follow-ons,
    the absent-partner probes). Nothing is lost by the two suppressions: field sync and
    creation for these keys are already owned in BOTH directions by the binding-aware
    outbound and inbound differ phases that run immediately afterwards.

    Scoped by ``(direction, action, provenance["source"], provenance["reason"])`` — the
    ``source`` check matters: invariant SEED mutations are prepended by
    ``compute_mutations``'s ``seed_mutations`` argument and carry
    ``provenance["source"] == "invariants"``, and a seed may legitimately reuse one of
    these reason strings. Keying on ``reason`` alone would drop it. Mutations that are
    plain dicts (legacy shape) have no ``provenance`` attribute and pass through untouched.
    Pure: returns a NEW list, never mutates in place.

    ``differ.compute_mutations`` itself is deliberately NOT modified — its documented
    local-vs-jira contract stays intact for callers that honour it, and the suppression
    lives at the one call site that does not.

    REMOVAL NOTE: if ``run_differs`` is ever migrated to pass REAL local state, this
    suppression must be DELETED in the same change — at that point both emissions become
    correct and dropping them would silently disable outbound create and outbound field
    sync.
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


def _accepts_synced_fields_out(fn: Any) -> bool:
    """Whether ``fn`` will accept the ``synced_fields_out`` kwarg (bug e6e9).

    True for the real ``applier.apply`` and any stub declaring ``**kwargs``; False for a
    stub with a fixed narrower signature, which keeps its pre-e6e9 call shape. An
    un-introspectable signature counts as NOT accepting it: a false positive raises
    TypeError and takes down the pass, a false negative only leaves the baseline
    advancing from the fetch as it does today.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "synced_fields_out" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _accepts_client(fn: Any) -> bool:
    """Whether ``fn`` accepts the ``client`` kwarg (RP-04 S3, AC1).

    True for the real ``applier.apply`` and any stub declaring ``**kwargs``; False for a
    stub with a fixed narrower signature, so the composed runtime's transport is
    forwarded only where it is accepted — mirroring the ``synced_fields_out`` tolerance
    so a narrow test stub is never handed an unexpected kwarg. An un-introspectable
    signature counts as NOT accepting it, leaving the applier's ambient ``_load_acli``
    fallback exactly as today.
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


def _write_facade_enabled() -> bool:
    """Whether the reconciler write facade (AC1 runtime threading) is ON.

    AC6 rollback toggle: setting ``REBAR_RECONCILER_WRITE_FACADE`` to a falsey value
    (``0``/``false``/``off``/``no``) restores the legacy ambient apply path — the pass
    skips composing/threading the runtime and ``applier.apply`` falls back to its own
    ambient ``_load_acli`` resolution. Default (unset) is ON, behavior-preserving.
    """
    raw = os.environ.get("REBAR_RECONCILER_WRITE_FACADE")  # read-via: rollback-toggle
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _resolve_pass_transport(ctx: Any):
    """Resolve the transport to hand the composed backend, honoring the applier's
    ``_load_acli`` seam so a test that patches it (or a stubbed transport) still drives
    the apply path. Returns ``None`` when the applier exposes no such seam (the composed
    runtime then builds the real provider transport from captured scope). A resolution
    failure re-raises for a persisting pass and degrades to ``None`` for a no-write pass.
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
    """Compose the ONE operation runtime for this pass and capture its backend + transport.

    The composed backend CAPTURES scope at compose time; threading its transport into the
    apply phase (as ``applier.apply(client=...)``) resolves the transport ONCE per pass
    rather than letting each apply re-resolve config ambiently via ``_load_acli``. The
    ``compose`` callable is passed in by the ``reconcile_once`` spine from the canonical
    runtime module, keeping this helper free of a back-edge to reconcile.py. The
    transport handed to ``build_backend`` comes from the applier's ``_load_acli`` seam.
    Existing tests that patch that seam keep controlling the client; when it is absent,
    the composed runtime builds the real
    provider transport from captured scope.

    Composition must not crash a read-only pass whose Jira scope is absent: on a
    compose/build failure we re-raise for a persisting (write) pass (fail closed) but fall
    back to the ambient path (``client=None``) for a no-write pass, so dry-run /
    preview passes keep working. Disabled entirely by AC6's toggle.
    """
    if not _write_facade_enabled():
        return
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
    """Advance every CONFIRMED binding's per-binding baseline to the LAST-SYNCED Jira state
    (story d6bd — the always-on successor to the retired dual-write shadow). Only
    confirmed bindings whose Jira key is in the current fetch window are advanced (an
    out-of-window key has no fresh value this pass); ``set_baseline`` filters to the
    mirrored fields. In-memory until the caller's ``save()`` persists them (ADR 0026).

    Two sources with different freshness (bug e6e9), applied in order: ``curr_snapshot``
    is the pass-START fetch, taken BEFORE the outbound apply, and is correct for every
    field rebar did not write; ``synced_fields`` is what rebar's own writes CONFIRMEDLY
    landed later in the SAME pass, so it is strictly fresher for those fields and is
    overlaid on top. Advancing from the fetch alone is the defect ``peer_state.
    merge_baseline`` documents, and ``synced_fields`` MUST carry only per-mutation
    confirmed writes — never "the pass ran".

    The overlay runs for a confirmed binding even when its key is OUT of the fetch window:
    our own write is direct evidence about the peer and, unlike a fetch, cannot be missing.
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
        print(
            f"RECON: baseline_overlay bindings={overlaid} pushed_bindings={len(synced)}",
            file=sys.stderr,
        )
    return advanced


def _advance_peer_parent(binding_store: Any, local_id: str, entry: Mapping[str, Any]) -> None:
    """Record the peer parent OBSERVED for one binding — and ONLY if it was observed.

    The evidence an inbound parent CLEAR requires (ticket 88d9). The observation test is
    ``"parent" in entry``: key PRESENT means the parent map answered for this issue, and an
    explicit ``None`` is then an authoritative "no parent". Key ABSENT is the whole unsafe set —
    ``get_parent_map`` degraded to ``{}`` on a REST failure, a truncated page walk, a
    cross-project issue — and MUST leave the prior observation untouched. Overwriting a good
    history with a failed read is what would let the orphaning incident recur by a longer route,
    so this is the load-bearing line, not a defensive nicety.

    getattr-guarded so a store predating the field is a no-op rather than an AttributeError.
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
