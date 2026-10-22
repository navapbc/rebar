#!/usr/bin/env python3
"""Applier: the outbound-batch *sequencer* + polymorphic ``apply()`` entry point.

``apply()`` selects between typed single-mutation dispatch (``_apply_typed``) and
the legacy batch path (``_apply_batch``) by argument type. ``_apply_batch`` is a
thin sequencer — resolve transport → cross-project guard → HEAD-drift recheck
loop → per-mutation dispatch + record → manifest-write tail — over machinery that
lives in sibling modules:

    - apply_base.py      — ApplyResult / mutation / _errors loaders + _direction_guard
    - apply_inbound.py   — inbound leaf appliers
    - apply_outbound.py  — outbound leaf appliers + HEAD-drift helpers
    - typed_dispatch.py  — the _LEAVES routing table + _apply_typed
    - batch_dispatch.py  — create_one / update_one / delete_one (one attempt each)
                           + JiraAPIError / RetryExhaustedError
    - apply_handlers.py  — the per-action batch handlers (create/update/delete)
                           + BatchApplyContext + dispatch_mutation
    - pass_io.py         — mapping/pass-record IO + the reschedule contract
    - rebar_id_audit.py  — the rebar-id label-write authorization guard

The names below are re-exported (see ``__all__``) so ``applier.<name>`` keeps
resolving for reconcile.py's getattr dispatch table and the test suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._backend import TicketTransport

import json
import logging
import sys
from functools import partial
from pathlib import Path

logger = logging.getLogger(__name__)


# Typed-mutation dispatch layer.
#
# The applier was originally written as a single batch-style apply(mutations,
# pass_id, ...) routine over dict-shaped mutations. The narrow-applier-matrix
# story introduces a typed Mutation value object (mutation.Mutation with
# MutationDirection / MutationAction enums) and a per-leaf dispatch registry
# (_LEAVES) so callers can route a single Mutation through exactly one
# direction/action handler.
#
# The two surfaces coexist:
#   - apply(mutation: Mutation, *, client=None) -> ApplyResult
#       Typed single-mutation dispatch via _LEAVES.
#   - apply(mutations: Sequence[Mutation], pass_id, repo_root=None) -> Path
#       Batch dispatch (manifest writer + HEAD-drift guard).
#
# Selection is by argument type at the top of apply().
# Foundational apply primitives live in apply_base.py (single-identity
# ApplyResult/mutation/_errors loaders + _direction_guard). Re-exported so the
# resident leaves, _apply_typed, and applier.<name> refs resolve.
# The cross-project safety guard + HEAD-drift detection cluster (the two exception
# types plus their two checks) lives in applier_drift_guard.py (split for
# module-size headroom, ticket 7153-e5ad-5e20-4ae9; the same seam-extraction the
# clusters above already went through). Re-exported so _apply_batch
# (resident)'s own bare-name references resolve, so batch_dispatch.py's
# ``applier.HeadDriftError`` attribute read resolves, and so
# ``applier.<name>``/``applier_mod.<name>`` keeps resolving for existing tests.
from rebar.reconciler.applier_drift_guard import (  # noqa: E402
    CrossProjectTargetError,
    HeadDriftError,
    _cross_project_targets,
    _recheck_drift,
)
from rebar.reconciler.apply_base import (  # noqa: E402
    _MUTATION_KEY,
    ApplyResult,
    DirectionMismatchError,
    MutationShape,
    RebarIdLabelWriteError,
    StatusMappingError,
    UnknownActionError,
    _direction_guard,
    _errors_module,
    _ErrorsModule,
    _load_errors_module,
    _load_mutation_module,
    _MutationModule,
)

# Per-action batch handlers + the per-pass context live in apply_handlers.py.
# Imported (not re-exported) — _apply_batch builds the BatchApplyContext the reroutes
# dispatch through, and records an unrouted batch dict via record_backstop_failure.
from rebar.reconciler.apply_handlers import (  # noqa: E402
    BatchApplyContext,
    record_backstop_failure,
)

# Inbound leaf appliers live in apply_inbound.py.
# Re-exported so _build_leaves (resident) binds them.
from rebar.reconciler.apply_inbound import (  # noqa: E402
    _apply_inbound_clean_label,
    _apply_inbound_conflict,
    _apply_inbound_create,
    _apply_inbound_repair_property,
    _apply_inbound_update,
    inbound_repair_property,
)

# Subject prefixes considered "benign" for HEAD-drift tolerance — i.e.,
# external writers that don't conflict with in-flight outbound mutations.
# Bug f058: parallel Claude sessions running `rebar transition` /
# `rebar create` / etc. emit `ticket: <VERB>` commits to the tickets
# branch during a reconciler pass. The suggestion subsystem emits
# `suggestion: RECORD`. Other reconciler passes emit `acquire lock` /
# `release lock`. Competing outbound writes emit `pass_record: <pass_id>`
# — the original concern the drift detector was built for — and remain
# non-benign.
# Outbound leaf appliers + HEAD-drift helpers live in apply_outbound.py.
# Re-exported so _build_leaves (resident) and _apply_batch's drift check resolve.
from rebar.reconciler.apply_outbound import (  # noqa: E402, F401
    _apply_outbound_conflict,
    _apply_outbound_create,
    _apply_outbound_delete,
    _apply_outbound_probe,
    _apply_outbound_update,
    _drift_is_benign,
    _get_commit_subject,
)

# Outbound batch dispatch lives in batch_dispatch.py. Re-exported so resident
# _apply_batch/apply()/outbound leaves and the patch.object(applier, 'JiraAPIError')
# tests resolve.
from rebar.reconciler.batch_dispatch import (  # noqa: E402
    JiraAPIError,
    RetryExhaustedError,
    _is_illegal_transition_400,
    _mutation_to_batch_dict,
    create_one,
    delete_one,
    merge_pass_tallies,
    reroute_migrated_plans,
    reroute_ordered_create_plans,
    update_one,
)

# Deferred conflict bug filing lives in conflict_bug_filing.py (ticket 4527):
# dedup tag per (local_id, jira_key) pair, 24h accumulation cap, abort-if-empty,
# --detected-by provenance. Imported under the historic private name so the
# monkeypatch seam tests rely on (applier._file_conflict_bug_ticket) survives —
# the deferred loop below resolves this module-global at call time.
from rebar.reconciler.conflict_bug_filing import (  # noqa: E402
    file_conflict_bug_ticket as _file_conflict_bug_ticket,
)

# Jira→local translation + local-event-store IO live in inbound_translate.py.
# Re-imported so the resident inbound leaves resolve them as module globals.
from rebar.reconciler.inbound_translate import (  # noqa: E402
    _BRIDGE_INTERNAL_TAG_PREFIXES,
    _JIRA_PRIORITY_MAP,
    _JIRA_TYPE_MAP,
    _LOCAL_STATUS_VALUES,
    _TICKET_REDUCER_MODULE,
    _VALID_PRIORITY_RANGE,
    _event_meta,
    _extract_name,
    _jira_key_to_local_id,
    _jira_status_to_local,
    _normalize_adf_body,
    _read_latest_status,
    _resolve_priority,
    _resolve_tracker_dir,
    _write_event_file,
    recover_status_label,
)

# Pass-write persistence + the reschedule contract live in pass_io.py.
# Re-exported so apply()/_apply_batch and __main__'s getattr(applier, ...) resolve.
from rebar.reconciler.pass_io import (  # noqa: E402
    EXIT_RESCHEDULE,
    RescheduleError,
    _load_mapping,
    _write_mapping_atomic,
    _write_mapping_json_atomic,
)

# ---------------------------------------------------------------------------
# rebar-id label write authorization contract
# ---------------------------------------------------------------------------
# rebar-id label-write authorization lives in rebar_id_audit.py.
# Re-exported so test_errors.py's and the label-writer tests' getattr(applier, ...)
# reads resolve; the batch route runs the guard from rebar_id_audit directly.
from rebar.reconciler.rebar_id_audit import (  # noqa: E402
    _AUTHORIZED_REBAR_ID_LABEL_ACTIONS,
    _AUTHORIZED_REBAR_ID_LABEL_WRITERS,
    _AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC,
    _audit_rebar_id_label_writes,
    _BatchAuditView,
    _is_rebar_id_label_write_mutation,
)
from rebar.reconciler.scratch_retention import snapshots_dir  # noqa: E402

# The typed-dispatch routing table + dispatcher live in typed_dispatch.py.
# Re-exported so apply() (resident) + test_leaves_registry_coverage resolve.
from rebar.reconciler.typed_dispatch import (  # noqa: E402
    _LEAF_NAMES,
    _LEAVES,
    _apply_typed,
    _build_leaves,
)


def _load_acli():
    """Return the configured backend's transport (a ``TicketTransport``, i.e. an
    ``AcliClient``) directly — routed through the Backend port (S4).

    Lazily imports ``load_config``/``select_backend`` to avoid import cycles and to
    keep standalone by-path loading working.
    """
    from rebar.config import compose_config
    from rebar.reconciler._backend_registry import select_backend

    return select_backend(compose_config()).transport


# Sentinel returned by snapshot_head when the repository has no resolvable
# ref — i.e., neither a tickets branch nor any HEAD commit. Treated as a
# stable, never-equal-to-real-SHA value by drift-detection callers.
EMPTY_REPO_SENTINEL = "EMPTY_REPO"


def snapshot_head(repo_root: Path) -> str:
    """Return the current HEAD SHA of the tickets branch.

    Falls back to HEAD of the current branch when the tickets ref is absent
    (e.g., in a fresh test repo that has no orphan tickets branch yet). A
    repository with neither (``git init`` with no commits) returns
    ``EMPTY_REPO_SENTINEL``, so the drift guard treats every comparison as
    stable until the first commit lands (F9).
    """
    from rebar.config import tickets_branch
    from rebar.reconciler import git_adapter

    branch = tickets_branch(repo_root)  # configured tracker.branch (default "tickets")
    result = git_adapter.rev_parse(repo_root, branch)
    if result.returncode != 0:
        result = git_adapter.rev_parse(repo_root, "HEAD")
        if result.returncode != 0:
            return EMPTY_REPO_SENTINEL
    return result.stdout.strip()


def _typed_batch(mutations, ticket_plans) -> list:
    """Return the batch as a list; raise ``TypeError`` on a plan-less or untyped batch.

    The batch form of :func:`apply` takes typed ``Mutation``s only, plus the pass's
    ``ticket_plans`` (from ``ticket_planner.plan_pass``): the plans route every
    mutation, and since ADR 0103 S3 there is no legacy per-mutation loop to fall back
    to. Checking up front means a bad batch fails before any write.
    """
    if ticket_plans is None:
        raise TypeError("apply() batch form requires ticket_plans (ticket_planner.plan_pass)")
    batch = list(mutations or [])
    for m in batch:
        if not isinstance(m, MutationShape):
            raise TypeError(
                f"apply() batch form takes typed Mutations only, got {type(m).__name__}"
            )
    return batch


# Pass-planning policy (mode caps, suppression, manifest) lives in apply_planning.py.
# Re-exported so apply() (resident) calls them + the _mode_sort_key reads resolve.
from rebar.reconciler.apply_planning import (  # noqa: E402
    _emit_mode_manifest,
    _load_manifest_renderer,
    _load_mode_module,
    _mode_sort_key,
    _partition_by_mode_cap,
    _SuppressionIndex,
)

# Re-export facade. applier imports these names from its sibling leaf/IO modules
# solely so ``applier.<name>`` and ``from rebar.reconciler.applier import <name>``
# keep resolving for reconcile.py's getattr dispatch table and the test suite.
# Listing them in ``__all__`` documents that public surface and marks the imports
# as intentional re-exports.
__all__ = [
    "EXIT_RESCHEDULE",
    "_AUTHORIZED_REBAR_ID_LABEL_ACTIONS",
    "_AUTHORIZED_REBAR_ID_LABEL_WRITERS",
    "_AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC",
    "_BRIDGE_INTERNAL_TAG_PREFIXES",
    "_JIRA_PRIORITY_MAP",
    "_JIRA_TYPE_MAP",
    "_LEAF_NAMES",
    "_LEAVES",
    "_LOCAL_STATUS_VALUES",
    "_MUTATION_KEY",
    "_TICKET_REDUCER_MODULE",
    "_VALID_PRIORITY_RANGE",
    "ApplyResult",
    "CrossProjectTargetError",
    "DirectionMismatchError",
    "HeadDriftError",
    "JiraAPIError",
    "RebarIdLabelWriteError",
    "RescheduleError",
    "RetryExhaustedError",
    "StatusMappingError",
    "UnknownActionError",
    "_BatchAuditView",
    "_ErrorsModule",
    "_MutationModule",
    "_apply_inbound_clean_label",
    "_apply_inbound_conflict",
    "_apply_inbound_create",
    "_apply_inbound_repair_property",
    "_apply_inbound_update",
    "_apply_outbound_conflict",
    "_apply_outbound_create",
    "_apply_outbound_delete",
    "_apply_outbound_probe",
    "_apply_outbound_update",
    "_audit_rebar_id_label_writes",
    "_build_leaves",
    "_cross_project_targets",
    "_direction_guard",
    "_errors_module",
    "_event_meta",
    "_extract_name",
    "_is_illegal_transition_400",
    "_is_rebar_id_label_write_mutation",
    "_jira_key_to_local_id",
    "_jira_status_to_local",
    "_load_errors_module",
    "_load_manifest_renderer",
    "_load_mapping",
    "_load_mode_module",
    "_load_mutation_module",
    "_mode_sort_key",
    "_normalize_adf_body",
    "_read_latest_status",
    "_recheck_drift",
    "_resolve_priority",
    "_resolve_tracker_dir",
    "_write_event_file",
    "_write_mapping_atomic",
    "_write_mapping_json_atomic",
    "create_one",
    "delete_one",
    "inbound_repair_property",
    "recover_status_label",
    "update_one",
]


def apply(
    mutations=None,
    pass_id: str | None = None,
    repo_root: Path | None = None,
    *,
    client: TicketTransport | None = None,
    mode=None,
    binding_store=None,
    persist: bool = True,
    max_changes: int | None = None,
    route: str | None = None,
    abort_check=None,
    synced_fields_out=None,
    ticket_plans=None,
):
    """Polymorphic dispatch entry point.

    Two call shapes:
      1. Typed single-mutation:  apply(mutation, *, client=None) -> ApplyResult
      2. Batch:                   apply(mutations: Sequence[Mutation], pass_id, ...) -> Path
    Selection is by argument type at the top of the function. The batch form
    raises ``TypeError`` when any element is not a typed ``Mutation``.

    ``synced_fields_out`` (bug e6e9) is an optional out-parameter dict the batch path
    fills with ``local_id -> {vendor_field: value}`` for every outbound write CONFIRMED
    to have landed, so ``reconcile._advance_baselines`` can advance the ADR-0026 baseline
    to the last-SYNCED value rather than the pass-start fetch. Left untouched when None
    (and by the typed / dry-run / no-write paths, which issue no batch writes), so the
    caller sees an empty map and falls back to today's fetch-only advance.
    """
    # REB-3115 S5 T1: typed single-mutation dispatch via explicit declared protocol.
    if isinstance(mutations, MutationShape):
        return _apply_typed(
            mutations, client=client, repo_root=repo_root, binding_store=binding_store
        )

    if pass_id is None:
        raise TypeError("apply() legacy batch form requires pass_id as the second argument")
    mutations = _typed_batch(mutations, ticket_plans)

    # Mode-cap enforcement (story 286b): coerce mode + partition into applied/deferred.
    mode, mode_mod, mutations_input, deferred_for_manifest = _partition_by_mode_cap(
        mode, mutations, max_changes=max_changes
    )

    # Direction-aware dispatch (defect #8): inbound Mutations route through
    # _apply_typed per-mutation; outbound ones go to the legacy _apply_batch.
    mutations_list = list(mutations_input)

    def _direction_of(m) -> str:
        d = getattr(m, "direction", None)
        return str(getattr(d, "value", d) or "")

    inbound_typed: list = []
    outbound_typed: list = []
    for m in mutations_list:
        if _direction_of(m) == "inbound":
            inbound_typed.append(m)
        else:
            outbound_typed.append(m)
    # Convert every outbound Mutation to its batch dict now, before any inbound write, so
    # a malformed outbound mutation (e.g. a plain-dict create payload) fails the pass
    # before any effect.
    outbound_list = [_mutation_to_batch_dict(m) for m in outbound_typed]

    # suppress_pair follow-on contract (story bd19): a leaf emitting
    # follow_on={'kind':'suppress_pair',...} drops subsequent inbound mutations
    # for either id AND outbound batch entries for the jira_key this pass.
    suppression = _SuppressionIndex()

    # Obtain the backend transport for inbound leaves that write back to Jira (S4:
    # _load_acli now returns the configured backend's transport directly).
    if client is None and inbound_typed:
        client = _load_acli()
        logger.info(
            "inbound dispatch: created AcliClient for %d inbound mutations "
            "(JIRA_URL=%s, JIRA_USER=%s)",
            len(inbound_typed),
            getattr(client, "jira_url", None) or "<unset>",
            getattr(client, "user", None) or "<unset>",
        )

    # Deferred bug-filing directives from inbound conflict leaves, processed
    # AFTER _apply_batch to keep the apply path commit-free (bug d822).
    pending_bug_tickets: list[dict] = []
    inbound_applied_count = 0

    for mut in inbound_typed:
        if not persist:
            break
        # Per-mutation lost-lease checkpoint (epic dust-troth-naval): abort_check
        # raises (ReconcileLockLost) if the ref-lock heartbeat lost the lease, so a
        # displaced pass stops writing immediately. Defaults to None (no-op).
        if abort_check is not None:
            abort_check()
        if suppression.is_suppressed(getattr(mut, "target", "")):
            continue
        result = _apply_typed(mut, client=client, repo_root=repo_root, binding_store=binding_store)
        inbound_applied_count += 1
        result_payload = getattr(result, "payload", None) if result is not None else None
        follow_on = result_payload.get("follow_on") if isinstance(result_payload, dict) else None
        if isinstance(follow_on, dict) and follow_on.get("kind") == "suppress_pair":
            suppression.record(follow_on.get("local_id", ""), follow_on.get("jira_key", ""))
        pending = (
            result_payload.get("pending_bug_ticket") if isinstance(result_payload, dict) else None
        )
        if isinstance(pending, dict):
            pending_bug_tickets.append(pending)

    print(  # noqa: T201
        f"RECON: typed_inbound_dispatched count={len(inbound_typed)} "
        f"suppressed_pairs={len(suppression.suppressed_pairs)}",
        file=sys.stderr,
    )

    # Outbound: route the converted batch dicts through the legacy batch path.
    if suppression.suppressed_pairs:
        outbound_list = [
            d for d in outbound_list if not suppression.is_suppressed(d.get("key", ""))
        ]
    is_dry_run = mode_mod is not None and mode == mode_mod.Mode.DRY_RUN
    manifest_path = None
    try:
        if not is_dry_run and persist:
            manifest_path = _apply_batch(
                outbound_list,
                pass_id,
                repo_root=repo_root,
                binding_store=binding_store,
                abort_check=abort_check,
                synced_fields_out=synced_fields_out,
                ticket_plans=ticket_plans,
            )
    finally:
        if pending_bug_tickets and not is_dry_run:
            from rebar._engine import in_process_cli

            cli_path = Path(in_process_cli())
            for pending in pending_bug_tickets:
                try:
                    _file_conflict_bug_ticket(cli_path, pending)
                except Exception as exc:  # noqa: BLE001 — best-effort deferred bug filing must not fail pass
                    print(  # noqa: T201
                        f"deferred_bug_filing_failed: "
                        f"local_id={pending.get('local_id')!r} "
                        f"jira_key={pending.get('jira_key')!r} err={exc!r}",
                        file=sys.stderr,
                    )

    # Mode-specific manifest emission (story 286b): the planner returns an
    # (action, value) sentinel so this shell performs the early returns.
    if mode_mod is not None:
        action, value = _emit_mode_manifest(
            mode,
            mode_mod,
            mutations_list,
            deferred_for_manifest,
            pass_id,
            manifest_path,
            repo_root,
            persist,
            max_changes,
            route,
            inbound_applied_count + len(outbound_list) if route == "sync" and persist else None,
            ticket_plans=ticket_plans,
        )
        if action == "RETURN":
            return value
        manifest_path = value

    return manifest_path


def _apply_batch(
    mutations: list[dict],
    pass_id: str,
    repo_root: Path | None = None,
    binding_store=None,
    abort_check=None,
    synced_fields_out=None,
    *,
    ticket_plans,
) -> Path:
    """Batch dispatch: route dict mutations by ticket plan and write a flat-JSON manifest.

    ``ticket_plans`` is required: the create reroute and the coordinator reroute
    dispatch every planned dict. A dict no plan routes (a plan mixing a create with a
    non-create action) is recorded failed without a vendor call.

    Performs HEAD-pin drift detection before each mutation: captures the
    tickets-branch HEAD SHA before the first mutation, then re-checks before
    each subsequent mutation. If the HEAD changes mid-pass, raises HeadDriftError
    and aborts without issuing further Jira calls.

    Empty mutations list is a no-op fast path (no HEAD check invoked).

    Args:
        mutations: List of mutation dicts, each with at least an "action" field
                   ("create", "update", or "delete").
        pass_id:   Unique identifier for this reconciliation pass.
        repo_root: Repository root directory. Defaults to four levels above this file.
        synced_fields_out: Optional dict filled with ``local_id -> {field: value}`` for
                   the outbound writes that CONFIRMEDLY landed (bug e6e9). Populated from
                   the batch context AFTER the dispatch loop, so a HeadDriftError abort
                   contributes only the mutations that actually completed before it.

    Returns:
        Path to the written manifest file.

    Raises:
        HeadDriftError:   When the tickets-branch HEAD changes between mutations,
                          indicating a concurrent write by another process.
        RescheduleError:  When the write path signals reschedule. A health event
                          JSON is emitted to stderr before the raise. No
                          retry-counter file is written to disk; the next pass
                          starts fresh.
    """
    if repo_root is None:
        from rebar.config import reconciler_repo_root as _owned_repo_root

        repo_root = _owned_repo_root()

    # S4: _load_acli now returns the configured backend's transport directly. The
    # transport (an AcliClient) already carries the resolved connection settings
    # (jira_url/user/api_token and jira_project, defaulting to "DIG" to satisfy ACLI
    # on every CREATE — bug 4fa9-0846-519e-4c30), so no inline construction is needed.
    client = _load_acli()

    mutations_with_outcomes: list[dict] = []

    # Fast path: empty mutation list — skip HEAD check entirely
    if not mutations:
        manifest = {
            "pass_id": pass_id,
            "mutation_count": 0,
            "mutations": [],
            "events": [],
        }
        manifest_dir = snapshots_dir(repo_root)
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"{pass_id}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return manifest_path

    # Resolve the configured project scope for the cross-project safety guard
    # (bug 626d) via the Backend port's ``project`` (ticket 97f2/bbf1) — the
    # DIG-defaulted write scope matches the create client. Read off the backend
    # (not the transport) so a test fake transport without a jira_project
    # attribute still works. Resolved HERE (past the empty-mutations fast path)
    # so a zero-mutation pass builds no extra backend.
    from rebar.config import compose_config
    from rebar.reconciler import projects_store
    from rebar.reconciler._backend_registry import select_backend

    _project = select_backend(compose_config()).project
    # Story d19d: with a seeded many-to-many mapping, the allowed write scope is the
    # store's whole project SET, not the single construction-time default. An unseeded
    # store (no projects.json) yields no keys, so fall back to the single ``_project``
    # string — preserving the bug-626d single-project guard exactly.
    _allowed: str | list[str] = list(projects_store.read_projects(repo_root).keys()) or _project
    _scope_display = (
        ", ".join(repr(p) for p in _allowed) if isinstance(_allowed, list) else repr(_allowed)
    )

    # Cross-project safety guard (bug 626d): refuse — BEFORE issuing any Jira
    # write — to push outbound updates/deletes at issues outside the allowed
    # project scope. Stale bindings/labels from a prior sync to another project
    # would otherwise silently mutate the wrong project's issues. Fail-closed:
    # abort the whole pass (no partial writes) so a misconfiguration cannot leak.
    offenders = _cross_project_targets(mutations, _allowed)
    if offenders:
        sample = ", ".join(f"{k}(→{p})" for k, p in offenders[:5])
        more = " …" if len(offenders) > 5 else ""
        raise CrossProjectTargetError(
            f"refusing to apply {len(offenders)} outbound mutation(s) targeting a "
            f"Jira project outside the configured scope {_scope_display}: {sample}{more}. "
            f"The store carries bindings/labels for another project; re-target it "
            f"(clear stale bindings + strip foreign id labels) before "
            f"syncing — see docs/jira-sync-setup.md."
        )

    ctx = BatchApplyContext(
        client=client,
        repo_root=repo_root,
        pass_id=pass_id,
        binding_store=binding_store,
    )

    # Pin HEAD before first mutation; the reroutes then sequence — drift recheck →
    # dispatch → record — one mutation at a time.
    head_pin_cell = [snapshot_head(repo_root)]
    # LIVE cutover (RP-03 S3 T3): reroute migrated non-create plans inside the drift try.
    create_tally = None
    cutover_tally = None
    try:
        create_tally, mutations = reroute_ordered_create_plans(
            ticket_plans=ticket_plans,
            mutations=mutations,
            ctx=ctx,
            outcomes_sink=mutations_with_outcomes,
            before_dispatch=partial(
                _dispatch_checkpoint, abort_check, snapshot_head, repo_root, head_pin_cell
            ),
        )
        cutover_tally, mutations = reroute_migrated_plans(
            ticket_plans=ticket_plans,
            mutations=mutations,
            ctx=ctx,
            abort_check=abort_check,
            recheck_drift=_recheck_drift,
            snapshot_head=snapshot_head,
            repo_root=repo_root,
            head_pin_cell=head_pin_cell,
            outcomes_sink=mutations_with_outcomes,
        )
        for mutation in mutations:
            _record_unrouted(mutation, ctx, mutations_with_outcomes)
    except HeadDriftError:
        # Emit abort event as structured log and re-raise for the caller.
        print(  # noqa: T201
            json.dumps(
                {
                    "kind": "abort_due_to_drift",
                    "pass_id": pass_id,
                    "head_pin": head_pin_cell[0],
                    "mutations_completed": len(mutations_with_outcomes),
                }
            ),
            file=sys.stderr,
        )
        raise

    if synced_fields_out is not None:
        synced_fields_out.update(ctx.synced_fields)

    manifest = {
        "pass_id": pass_id,
        "mutation_count": len(mutations_with_outcomes),
        "mutations": mutations_with_outcomes,
        "events": ctx.events_list,
    }
    combined_tally = merge_pass_tallies(create_tally, cutover_tally)
    if combined_tally is not None:
        manifest["cutover_tally"] = combined_tally

    manifest_dir = snapshots_dir(repo_root)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{pass_id}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return manifest_path


def _dispatch_checkpoint(abort_check, snapshot_head, repo_root, head_pin_cell) -> None:
    """Run the per-mutation gates before one create is dispatched.

    The lost-lease checkpoint (epic dust-troth-naval) raises if the ref-lock heartbeat
    lost the lease, and the HEAD re-check raises ``HeadDriftError`` if HEAD moved since
    the pin. The retired legacy loop ran both before every dispatch; the create reroute
    now calls this before each create it issues.
    """
    if abort_check is not None:
        abort_check()
    head_pin_cell[0] = _recheck_drift(snapshot_head, repo_root, head_pin_cell[0])


def _record_unrouted(
    mutation: dict, ctx: BatchApplyContext, mutations_with_outcomes: list[dict]
) -> None:
    """Record a batch dict that no ticket plan routed, dispatching nothing.

    The create and coordinator reroutes consume every planned dict. One left over (a
    plan mixing a create with a non-create action) has no route since ADR 0103 S3
    retired the legacy per-mutation loop, so it is recorded as a per-mutation failure
    (a ``bridge_alerts`` entry + an ``error`` outcome) rather than silently dropped.
    """
    action = mutation.get("action", "")
    result = record_backstop_failure(
        mutation, LookupError(f"unrouted mutation: {action}"), action, ctx
    )
    mutations_with_outcomes.append(result.outcome)
    _print_batch_recon(action, result.outcome, soft_failed=True)


def _print_batch_recon(action: str, outcome: dict, *, soft_failed: bool) -> None:
    """Emit the per-mutation RECON line (bug b859 Part 0c) so operators see which
    dispatch actually ran without parsing the manifest.

    Story E (2359): update outcomes carry the sub-op applied counts + silent-noop
    flag suffix. The 404 / assignee soft-failures (soft_failed) record and return
    before that telemetry is computed, so they omit the suffix — matching the
    pre-split output.
    """
    _outcome_key = outcome.get("key") or outcome.get("local_id") or "<unknown>"
    _outcome_err = outcome.get("error")
    if action == "update" and not soft_failed:
        _recon_subops = (
            f" links_applied={outcome.get('links_applied', 0)}"
            f" links_failed={outcome.get('links_failed', 0)}"
            f" comments_applied={outcome.get('comments_applied', 0)}"
            f" labels_applied={outcome.get('labels_applied', 0)}"
            f" silent_noop={outcome.get('silent_noop', [])!r}"
        )
    else:
        _recon_subops = ""
    print(  # noqa: T201
        f"RECON: batch_outcome action={action} key={_outcome_key} "
        f"error={_outcome_err!r}{_recon_subops}",
        file=sys.stderr,
    )
