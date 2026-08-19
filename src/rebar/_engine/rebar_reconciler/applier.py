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
    - batch_dispatch.py  — create_one / update_one / delete_one + _call_with_retry
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
    from collections.abc import Iterable

    from ._backend import TicketTransport

import importlib.util
import json
import logging
import re
import sys
import urllib.error
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
#   - apply(mutations: list[dict], pass_id, repo_root=None) -> Path
#       Legacy batch dispatch (manifest writer + HEAD-drift guard).
#
# Selection is by argument type at the top of apply().
# Foundational apply primitives live in apply_base.py (single-identity
# ApplyResult/mutation/_errors loaders + _direction_guard). Re-exported so the
# resident leaves, _apply_typed, and applier.<name> refs resolve.
from rebar_reconciler.apply_base import (  # noqa: E402
    _MUTATION_KEY,
    ApplyResult,
    DirectionMismatchError,
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
# Imported (not re-exported) — _apply_batch's per-mutation step dispatches through
# dispatch_mutation; the handlers wrap batch_dispatch's create/update/delete_one.
from rebar_reconciler.apply_handlers import (  # noqa: E402
    BatchApplyContext,
    dispatch_mutation,
    record_backstop_failure,
)

# Inbound leaf appliers live in apply_inbound.py.
# Re-exported so _build_leaves (resident) binds them.
from rebar_reconciler.apply_inbound import (  # noqa: E402
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
from rebar_reconciler.apply_outbound import (  # noqa: E402
    _apply_outbound_conflict,
    _apply_outbound_create,
    _apply_outbound_delete,
    _apply_outbound_probe,
    _apply_outbound_update,
    _drift_is_benign,
    _get_commit_subject,
)

# Outbound batch dispatch + Jira-call retry live in batch_dispatch.py.
# Re-exported so resident _apply_batch/apply()/outbound leaves and the
# patch.object(applier, '_call_with_retry'/'JiraAPIError') tests resolve.
from rebar_reconciler.batch_dispatch import (  # noqa: E402
    JiraAPIError,
    RetryExhaustedError,
    _call_with_retry,
    _is_illegal_transition_400,
    _mutation_to_batch_dict,
    create_one,
    delete_one,
    update_one,
)

# Deferred conflict bug filing lives in conflict_bug_filing.py (ticket 4527):
# dedup tag per (local_id, jira_key) pair, 24h accumulation cap, abort-if-empty,
# --detected-by provenance. Imported under the historic private name so the
# monkeypatch seam tests rely on (applier._file_conflict_bug_ticket) survives —
# the deferred loop below resolves this module-global at call time.
from rebar_reconciler.conflict_bug_filing import (  # noqa: E402
    file_conflict_bug_ticket as _file_conflict_bug_ticket,
)

# Jira→local translation + local-event-store IO live in inbound_translate.py.
# Re-imported so the resident inbound leaves resolve them as module globals.
from rebar_reconciler.inbound_translate import (  # noqa: E402
    _BRIDGE_INTERNAL_TAG_PREFIXES,
    _JIRA_PRIORITY_MAP,
    _JIRA_TYPE_MAP,
    _LOCAL_STATUS_VALUES,
    _REBAR_STATUS_LABEL_TO_LOCAL,
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
)

# Pass-write persistence + the reschedule contract live in pass_io.py.
# Re-exported so apply()/_apply_batch and __main__'s getattr(applier, ...) resolve.
from rebar_reconciler.pass_io import (  # noqa: E402
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
# Re-exported so _apply_typed/_apply_batch (resident) and test_errors.py's
# getattr(applier, ...) reads resolve.
from rebar_reconciler.rebar_id_audit import (  # noqa: E402
    _AUTHORIZED_REBAR_ID_LABEL_ACTIONS,
    _AUTHORIZED_REBAR_ID_LABEL_WRITERS,
    _AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC,
    _audit_rebar_id_label_writes,
    _BatchAuditView,
    _is_rebar_id_label_write_mutation,
)

# The typed-dispatch routing table + dispatcher live in typed_dispatch.py.
# Re-exported so apply() (resident) + test_leaves_registry_coverage resolve.
from rebar_reconciler.typed_dispatch import (  # noqa: E402
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
    from rebar_reconciler._backend_registry import select_backend

    return select_backend(compose_config()).transport


class HeadDriftError(Exception):
    """Raised when the tickets-branch HEAD changes mid-pass, indicating concurrent write."""


class CrossProjectTargetError(Exception):
    """Raised when an outbound mutation targets a Jira project other than jira.project.

    A fail-closed safety guard (bug 626d): stale bindings/labels from a prior sync to
    another project would otherwise silently push updates/deletes at the wrong
    project's issues. Raised pre-flight (before any Jira write) so a misconfiguration
    cannot leak even a single mutation.
    """


# A real Jira issue key: PROJECTKEY-NUMBER (e.g. "DIG-1234"). Create mutations
# carry a local-id placeholder here, not a real key, so they don't match.
_JIRA_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)-\d+$")


def _cross_project_targets(
    mutations: list[dict], allowed: str | Iterable[str]
) -> list[tuple[str, str]]:
    """Return ``(key, project)`` for outbound update/delete mutations whose target
    Jira key belongs to a project OUTSIDE the allowed set.

    ``allowed`` is either a single configured project (a bare string — the legacy
    single-project case) or the store's project SET (story d19d, many-to-many): a
    mutation targeting any project in the set passes. Creates are excluded — their
    ``key`` is a local-id placeholder and their project is resolved on the create
    path, not here. Inbound mutations are excluded. An empty/unset ``allowed``
    disables the check (returns ``[]``) so it never fires on shims that don't
    configure a project.
    """
    if isinstance(allowed, str):
        allowed_set = {allowed.upper()} if allowed else set()
    else:
        allowed_set = {str(p).upper() for p in allowed if p}
    if not allowed_set:
        return []
    offenders: list[tuple[str, str]] = []
    for m in mutations:
        if (m.get("direction") or "outbound") == "inbound":
            continue
        if m.get("action") not in ("update", "delete"):
            continue
        key = str(m.get("key") or m.get("local_id") or "")
        match = _JIRA_KEY_RE.match(key)
        if not match:
            continue
        proj = match.group(1).upper()
        if proj not in allowed_set:
            offenders.append((key, match.group(1)))
    return offenders


def _load_concurrency():
    """Load _concurrency module via importlib."""
    concurrency_path = Path(__file__).parent / "_concurrency.py"
    spec = importlib.util.spec_from_file_location("_concurrency", concurrency_path)
    if spec is None:
        raise FileNotFoundError(f"_concurrency.py not found at {concurrency_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_concurrency", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Pass-planning policy (mode caps, suppression, manifest) lives in apply_planning.py.
# Re-exported so apply() (resident) calls them + the _mode_sort_key reads resolve.
from rebar_reconciler.apply_planning import (  # noqa: E402
    _emit_mode_manifest,
    _load_manifest_renderer,
    _load_mode_module,
    _mode_sort_key,
    _partition_by_mode_cap,
    _SuppressionIndex,
)

# Re-export facade. applier imports these names from its sibling leaf/IO modules
# solely so ``applier.<name>`` and ``from rebar_reconciler.applier import <name>``
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
    "_REBAR_STATUS_LABEL_TO_LOCAL",
    "_TICKET_REDUCER_MODULE",
    "_VALID_PRIORITY_RANGE",
    "ApplyResult",
    "DirectionMismatchError",
    "JiraAPIError",
    "RebarIdLabelWriteError",
    "RescheduleError",
    "RetryExhaustedError",
    "StatusMappingError",
    "UnknownActionError",
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
    "_build_leaves",
    "_call_with_retry",
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
    "_mode_sort_key",
    "_normalize_adf_body",
    "_read_latest_status",
    "_resolve_priority",
    "_resolve_tracker_dir",
    "_write_event_file",
    "_write_mapping_atomic",
    "_write_mapping_json_atomic",
    "create_one",
    "delete_one",
    "inbound_repair_property",
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
):
    """Polymorphic dispatch entry point.

    Two call shapes:
      1. Typed single-mutation:  apply(mutation, *, client=None) -> ApplyResult
      2. Legacy batch:            apply(mutations: list[dict], pass_id, ...) -> Path
    Selection is by argument type at the top of the function.

    ``synced_fields_out`` (bug e6e9) is an optional out-parameter dict the batch path
    fills with ``local_id -> {vendor_field: value}`` for every outbound write CONFIRMED
    to have landed, so ``reconcile._advance_baselines`` can advance the ADR-0026 baseline
    to the last-SYNCED value rather than the pass-start fetch. Left untouched when None
    (and by the typed / dry-run / no-write paths, which issue no batch writes), so the
    caller sees an empty map and falls back to today's fetch-only advance.
    """
    mut_mod = _load_mutation_module()
    if isinstance(mutations, mut_mod.Mutation) or (
        type(mutations).__name__ == "Mutation"
        and hasattr(mutations, "direction")
        and hasattr(mutations, "action")
    ):
        return _apply_typed(
            mutations, client=client, repo_root=repo_root, binding_store=binding_store
        )

    if pass_id is None:
        raise TypeError("apply() legacy batch form requires pass_id as the second argument")

    # Mode-cap enforcement (story 286b): coerce mode + partition into applied/deferred.
    mode, mode_mod, mutations_input, deferred_for_manifest = _partition_by_mode_cap(
        mode, mutations, max_changes=max_changes
    )

    # Direction-aware dispatch (defect #8): inbound typed Mutations route through
    # _apply_typed per-mutation; outbound/untyped go to the legacy _apply_batch.
    mutations_list = list(mutations_input)

    def _looks_like_mutation(m) -> bool:
        if isinstance(m, mut_mod.Mutation):
            return True
        return type(m).__name__ == "Mutation" and hasattr(m, "direction") and hasattr(m, "action")

    def _direction_of(m) -> str:
        d = getattr(m, "direction", None)
        return str(getattr(d, "value", d) or "")

    inbound_typed: list = []
    outbound_or_untyped: list = []
    for m in mutations_list:
        if _looks_like_mutation(m) and _direction_of(m) == "inbound":
            inbound_typed.append(m)
        else:
            outbound_or_untyped.append(m)

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

    print(
        f"RECON: typed_inbound_dispatched count={len(inbound_typed)} "
        f"suppressed_pairs={len(suppression.suppressed_pairs)}",
        file=sys.stderr,
    )

    # Outbound (or untyped dict): normalize typed Mutations to dicts so
    # _apply_batch can iterate, then route through the legacy batch path.
    outbound_list = [
        _mutation_to_batch_dict(m) if _looks_like_mutation(m) else m for m in outbound_or_untyped
    ]
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
            )
    finally:
        if pending_bug_tickets and not is_dry_run:
            from rebar._engine import in_process_cli

            cli_path = Path(in_process_cli())
            for pending in pending_bug_tickets:
                try:
                    _file_conflict_bug_ticket(cli_path, pending)
                except Exception as exc:  # noqa: BLE001 — best-effort deferred bug filing must not fail pass
                    print(
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
) -> Path:
    """Legacy batch dispatch: write a flat-JSON manifest for a list of dict mutations.

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
        RescheduleError:  When rebase_retry exhausts all write attempts
                          (kind='reject_and_reschedule').  A health event JSON is
                          emitted to stderr before the raise.  No retry-counter
                          file is written to disk; the next pass starts fresh.
    """
    if repo_root is None:
        from rebar.config import repo_root as _owned_repo_root

        repo_root = _owned_repo_root()

    # S4: _load_acli now returns the configured backend's transport directly. The
    # transport (an AcliClient) already carries the resolved connection settings
    # (jira_url/user/api_token and jira_project, defaulting to "DIG" to satisfy ACLI
    # on every CREATE — bug 4fa9-0846-519e-4c30), so no inline construction is needed.
    client = _load_acli()

    mutations_with_outcomes: list[dict] = []

    # Load concurrency module once (used both in the fast path and the main loop)
    concurrency = _load_concurrency()

    # Fast path: empty mutation list — skip HEAD check entirely
    if not mutations:
        manifest = {
            "pass_id": pass_id,
            "mutation_count": 0,
            "mutations": [],
            "events": [],
        }
        snapshots_dir = repo_root / "bridge_state" / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = snapshots_dir / f"{pass_id}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return manifest_path

    # Resolve the configured project scope for the cross-project safety guard
    # (bug 626d) via the Backend port's ``project`` (ticket 97f2/bbf1) — the
    # DIG-defaulted write scope matches the create client. Read off the backend
    # (not the transport) so a test fake transport without a jira_project
    # attribute still works. Resolved HERE (past the empty-mutations fast path)
    # so a zero-mutation pass builds no extra backend.
    from rebar.config import compose_config
    from rebar_reconciler import projects_store
    from rebar_reconciler._backend_registry import select_backend

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

    # Pin HEAD before first mutation, then sequence — drift recheck → dispatch →
    # record — one mutation at a time. The per-mutation step is extracted (see
    # _apply_one) so this loop body stays shallow and the abort-on-drift contract
    # reads at a glance.
    head_pin = concurrency.snapshot_head(repo_root)

    try:
        for mutation in mutations:
            # Per-mutation lost-lease checkpoint (epic dust-troth-naval): raises if
            # the ref-lock heartbeat lost the lease. Defaults to None (no-op).
            if abort_check is not None:
                abort_check()
            head_pin = _recheck_drift(concurrency, repo_root, head_pin)
            _apply_one(mutation, ctx, mutations_with_outcomes)
    except HeadDriftError:
        # Emit abort event as structured log and re-raise for the caller.
        print(
            json.dumps(
                {
                    "kind": "abort_due_to_drift",
                    "pass_id": pass_id,
                    "head_pin": head_pin,
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
        "mutation_count": len(mutations),
        "mutations": mutations_with_outcomes,
        "events": ctx.events_list,
    }

    snapshots_dir = repo_root / "bridge_state" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = snapshots_dir / f"{pass_id}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return manifest_path


def _recheck_drift(concurrency, repo_root: Path, head_pin: str) -> str:
    """Re-check the tickets-branch HEAD before a mutation; return the (possibly
    refreshed) pin, or raise HeadDriftError on a competing reconciler write.

    Bug f058: the tickets orphan branch is shared with the ticket CLI
    (auto-commits via rebar create / transition / etc.) and the suggestion
    subsystem. A parallel Claude session running `rebar transition <id> closed`
    triggers auto-compact, which commits `ticket: COMPACT <id>` to tickets — that
    doesn't conflict with the in-flight outbound mutations, but a strict-equality
    drift check would abort the pass. Resolution: inspect the intervening commit's
    subject. If it matches a benign external pattern (ticket-CLI, suggestion,
    pass-lock), refresh the pin and continue. Only raise HeadDriftError when the
    subject indicates a competing reconciler outbound write — the original intent
    of the detector.
    """
    current_head = concurrency.snapshot_head(repo_root)
    if current_head == head_pin:
        return head_pin
    drift_subject = _get_commit_subject(repo_root, current_head)
    if _drift_is_benign(drift_subject):
        # Benign external writer — accept the new HEAD and continue. Log so
        # operators can see the writer.
        print(
            f"tolerated_drift: {head_pin[:8]}→{current_head[:8]} subject={drift_subject!r}",
            file=sys.stderr,
        )
        return current_head
    raise HeadDriftError(f"drift: {head_pin[:8]}→{current_head[:8]} subject={drift_subject!r}")


def _apply_one(mutation: dict, ctx: BatchApplyContext, mutations_with_outcomes: list[dict]) -> None:
    """Audit-guard, dispatch, and record one mutation's outcome.

    The audit guard stays resident here (not in the handlers): it is the
    pre-dispatch authorization check, and the test suite reaches it via
    ``applier._audit_rebar_id_label_writes`` / ``applier._BatchAuditView``.
    """
    action = mutation.get("action", "")
    # Audit pass: extend the rebar-id label write guard to the legacy batch
    # dispatch path. create_one/update_one/delete_one all issue outbound Jira
    # writes, so each batch mutation maps to an outbound_<action> leaf for
    # guard-name purposes. Without this call, _audit_rebar_id_label_writes was
    # bypassed for every legacy dict-shaped mutation — only _apply_typed enforced
    # the contract.
    _audit_rebar_id_label_writes(f"outbound_{action}", [_BatchAuditView(mutation)])
    try:
        result = dispatch_mutation(mutation, ctx)
    except (HeadDriftError, RescheduleError, urllib.error.HTTPError):
        # Control-flow / fail-fast contracts — re-raise (see record_backstop_failure):
        # HeadDriftError (drift-abort), RescheduleError (rebase-retry exhausted), HTTPError
        # (404 soft-failed in the handler; non-404 deliberately propagates fail-fast).
        # Bug 449f-f9bf-be90-47fe: that 404 clause held for `update` ONLY — create/delete
        # wrapped their leaf in nothing, so a 404 from either escaped here and aborted the
        # pass (1 of 30 mutations applied, GHA run 30465914822). All three handlers now
        # share the soft-fail, so the invariant this arm asserts holds for every action.
        raise
    except Exception as exc:  # noqa: BLE001 — per-mutation failure backstop (records + continues)
        result = record_backstop_failure(mutation, exc, action, ctx)
    mutations_with_outcomes.append(result.outcome)
    _print_batch_recon(action, result.outcome, soft_failed=result.soft_failed)


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
    print(
        f"RECON: batch_outcome action={action} key={_outcome_key} "
        f"error={_outcome_err!r}{_recon_subops}",
        file=sys.stderr,
    )
