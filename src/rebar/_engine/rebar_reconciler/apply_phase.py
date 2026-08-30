#!/usr/bin/env python3
"""apply_phase.py — reconcile_once's apply phase, extracted from reconcile.py.

Ticket piscine-bullish-cowbird (module-size headroom): reconcile.py was at the
locked 800-line cap. ``apply_mutations`` (the store-compat guard + filter-scope
narrowing + status preflight + the single applier.apply dispatch, normalizing
its write / no-write / LIVE-tally return shapes) moved here verbatim.

Confirmed via monkeypatch/patch census (no test patches any of this function's
sub-dependencies — ``_build_filter_target_set``, ``_mutation_matches_filter``,
``preflight_status_mapping``, ``_accepts_synced_fields_out``, ``_accepts_client``,
``_accepts_ticket_plans`` — through ``reconcile.<name>`` expecting it to alter
``_apply_mutations``'s own behavior) that this phase can move WHOLESALE: no
dependency-injection wrapper is required, unlike the load/persist phases. Its
sub-dependencies are imported directly from their owning sibling modules
(``pass_support.py`` and ``reconcile_helpers.py``) rather than re-threaded
through reconcile.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

# Same standalone-load bootstrap idiom as reconcile.py: import lazy_load
# normally when package context exists, else load it by file path so this
# module keeps working when exec'd standalone.
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

_pass_support = lazy_load("pass_support", "pass_support.py")
_helpers = lazy_load("reconcile_helpers", "reconcile_helpers.py")

_build_filter_target_set = _pass_support._build_filter_target_set
_mutation_matches_filter = _pass_support._mutation_matches_filter
preflight_status_mapping = _pass_support.preflight_status_mapping
_accepts_synced_fields_out = _helpers._accepts_synced_fields_out
_accepts_client = _helpers._accepts_client
_accepts_ticket_plans = _helpers._accepts_ticket_plans


def apply_mutations(ctx: Any) -> None:
    """Apply phase: optional filter-scope narrowing + status preflight + the single
    applier.apply dispatch and normalize its write/no-write return shapes.
    Records manifest_path / nowrite_plan / the unfiltered count back onto ctx.
    """
    mutations = ctx.mutations
    filter_local_ids = ctx.filter_local_ids
    binding_store = ctx.binding_store
    pass_id = ctx.pass_id
    repo_root = ctx.repo_root
    target_mode = ctx.target_mode
    persist = ctx.persist
    applier = ctx.applier
    sync_logger = ctx.sync_logger

    # Story 21dd: the reconciler's outbound apply publishes ticket writes externally
    # (and to Jira), so fail CLOSED on a store this rebar cannot interpret BEFORE any
    # mutation. Guarded by `persist` so dry-run / cap-0 previews are excluded. RESOLVED,
    # not composed: a repo-root path would compat-check an unconfigured directory.
    if persist:
        from rebar._store.compat import check_store_compat
        from rebar.config import tracker_dir

        check_store_compat(tracker_dir(repo_root))

    # -------------------------------------------------------------------
    # Post-filter: when filter_local_ids is set, discard mutations that
    # target tickets outside the filter scope.  All three differs ran on
    # their full, unfiltered inputs (same code paths as production); only
    # the dispatch set is narrowed.
    # -------------------------------------------------------------------
    unfiltered_count = len(mutations)
    if filter_local_ids:
        target_set = _build_filter_target_set(filter_local_ids, binding_store)
        mutations = [m for m in mutations if _mutation_matches_filter(m, target_set)]
        print(
            f"filter: {unfiltered_count} mutations computed, "
            f"{len(mutations)} match filter ({len(filter_local_ids)} local IDs, "
            f"{len(target_set)} target keys)",
            file=sys.stderr,
        )
        sync_logger.log(
            "filter_applied",
            unfiltered=unfiltered_count,
            filtered=len(mutations),
            target_keys=len(target_set),
        )

    # Preflight: WARN (non-fatally) if any update mutation references a status
    # not present in config.local_to_jira_status. Runs exactly once per pass,
    # before any applier dispatch. It no longer aborts the pass (Facet 3): an
    # unmapped status flows to the applier and is recorded there as a
    # per-mutation failure rather than taking down every later mutation.
    preflight_status_mapping(mutations, repo_root)

    # Direction-aware dispatch lives inside applier.apply (PR #371 / defect
    # #8): the applier partitions typed Mutations by direction internally and
    # routes inbound via _apply_typed per-mutation, outbound via the batch
    # path. The previous reconcile_once-level typed/legacy split was a
    # parallel workaround for the same gap; with cap
    # enforcement landing in applier.apply (story 286b), all mutations must
    # flow through that single entry point so caps apply uniformly across
    # both directions.
    manifest_path = None
    nowrite_plan: dict | None = None
    # Bug c903: LIVE returns its applied/failed tally here instead of a Path.
    apply_tally: dict | None = None
    try:
        # Each optional kwarg below is forwarded ONLY when the resolved applier accepts
        # it (narrow test stubs use fixed signatures; an unexpected kwarg would raise
        # TypeError and abort the pass). mode is likewise only passed when target_mode is
        # set (cap enforcement requested). Refs: abort_check epic dust-troth-naval,
        # synced_fields_out bug e6e9, client RP-04 S3 AC1, ticket_plans RP-03 S2 T3.
        _abort_kw = {"abort_check": ctx.abort_check} if ctx.abort_check is not None else {}
        _synced_kw = (
            {"synced_fields_out": ctx.synced_fields}
            if _accepts_synced_fields_out(applier.apply)
            else {}
        )
        _client_kw = (
            {"client": ctx.runtime_transport}
            if ctx.runtime_transport is not None and _accepts_client(applier.apply)
            else {}
        )
        _ticket_plans_kw = (
            {"ticket_plans": getattr(ctx, "ticket_plans", None)}
            if getattr(ctx, "ticket_plans", None) is not None
            and _accepts_ticket_plans(applier.apply)
            else {}
        )
        if target_mode is None:
            manifest_path = applier.apply(
                mutations,
                pass_id,
                repo_root,
                binding_store=binding_store,
                **_abort_kw,
                **_synced_kw,
                **_client_kw,
            )
        else:
            _max_kw = {"max_changes": ctx.max_changes} if ctx.max_changes is not None else {}
            _route_kw = {"route": ctx.route} if ctx.route is not None else {}
            manifest_path = applier.apply(
                mutations,
                pass_id,
                repo_root,
                mode=target_mode,
                binding_store=binding_store,
                persist=persist,
                **_max_kw,
                **_route_kw,
                **_abort_kw,
                **_synced_kw,
                **_client_kw,
                **_ticket_plans_kw,
            )
    finally:
        # In no-write mode, apply() returns the computed plan dict instead of
        # a manifest Path. Capture it for the report and treat manifest_path
        # as None so no on-disk manifest is expected by the tally below.
        if not persist and isinstance(manifest_path, dict):
            nowrite_plan = manifest_path
            manifest_path = None
        # Bug c903: in LIVE (persist=True) apply() returns the applied/failed tally
        # read out of the manifest just before it was unlinked, NOT a Path. Route it
        # to ctx.apply_tally so _persist_and_log can count failures without an
        # on-disk manifest. Discriminated from the no-write plan dict above by
        # `persist`, which is False there and True here.
        elif persist and isinstance(manifest_path, dict):
            apply_tally = manifest_path
            manifest_path = None

    ctx.mutations = mutations
    ctx.unfiltered_count = unfiltered_count
    ctx.manifest_path = manifest_path
    ctx.nowrite_plan = nowrite_plan
    ctx.apply_tally = apply_tally
