#!/usr/bin/env python3
"""Applier: dispatches mutations to AcliClient and writes per-pass flat-JSON manifest.

TODO(follow-up): this module is 586 lines, exceeding the 500-line module-size
threshold. The intended split is:
    - mapping_io.py   — _load_mapping, _write_mapping_atomic, _write_mapping_json_atomic,
                        _persist_field_provenance
    - retry.py        — _call_with_retry, JiraAPIError, RetryExhaustedError
    - dispatchers.py  — create_one, update_one, delete_one
leaving applier.py with just the public apply() orchestrator + RescheduleError +
_handle_failed_write_result. The refactor was deferred from PR #290 because the
mechanical move + import-graph fixup is too large for the current PR. Track via
a follow-up bug ticket before the next applier-touching change.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import time
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)


def _rebar_env(name: str, default: str | None = None) -> str | None:
    """Read ``REBAR_<name>`` from the environment (DSO_* support removed).

    Local to this module: the reconciler modules are spec-loaded under test (where
    ``rebar_reconciler`` is the test-package shadow), so a cross-module import of a
    shared shim would not resolve.
    """
    return os.environ.get(f"REBAR_{name}", default)


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

# Inbound leaf appliers live in apply_inbound.py.
# Re-exported so _build_leaves (resident) binds them.
from rebar_reconciler.apply_inbound import (  # noqa: E402
    _apply_inbound_clean_label,
    _apply_inbound_conflict,
    _apply_inbound_create,
    _apply_inbound_delete,
    _apply_inbound_probe,
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

# Jira→local translation + local-event-store IO live in inbound_translate.py.
# Re-imported so the resident inbound leaves resolve them as module globals.
from rebar_reconciler.inbound_translate import (  # noqa: E402
    _ADF_KEY_APPLIER,
    _BRIDGE_INTERNAL_TAG_PREFIXES,
    _JIRA_PRIORITY_MAP,
    _JIRA_TYPE_MAP,
    _LOCAL_STATUS_VALUES,
    _REBAR_STATUS_LABEL_TO_LOCAL,
    _TICKET_REDUCER_MODULE,
    _VALID_PRIORITY_RANGE,
    _AdfModule_Applier,
    _event_meta,
    _extract_name,
    _jira_key_to_local_id,
    _jira_status_to_local,
    _load_adf_module,
    _normalize_adf_body,
    _read_latest_status,
    _resolve_priority,
    _resolve_tracker_dir,
    _write_event_file,
)


def _file_conflict_bug_ticket(cli_path: Path, title: str, description: str, parent_id: str) -> str:
    """Spawn the ticket CLI as a subprocess to file a bug ticket.

    Returns the canonical bug id on success, '' otherwise. Isolated as its
    own function so tests can monkeypatch this single seam without touching
    the broader subprocess module (which is used by _concurrency).
    """
    import subprocess

    if not cli_path.exists():
        return ""
    cmd: list[str] = [
        str(cli_path),
        "create",
        "bug",
        title,
        "-d",
        description,
    ]
    if parent_id:
        cmd.extend(["--parent", parent_id])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    if res.returncode != 0:
        return ""
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# The typed-dispatch routing table + dispatcher live in typed_dispatch.py.
# Re-exported so apply() (resident) + test_leaves_registry_coverage resolve.
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

# Pass-write persistence + the reschedule contract live in pass_io.py.
# Re-exported so apply()/_apply_batch and __main__'s getattr(applier, ...) resolve.
from rebar_reconciler.pass_io import (  # noqa: E402
    EXIT_RESCHEDULE,
    RescheduleError,
    _handle_failed_write_result,
    _load_alert_store,
    _load_conflict_resolver,
    _load_mapping,
    _persist_field_provenance,
    _write_mapping_atomic,
    _write_mapping_json_atomic,
    _write_pass_record,
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
    _get_rebar_id_guard_mode_from_config,
    _is_rebar_id_label_write_mutation,
)
from rebar_reconciler.typed_dispatch import (  # noqa: E402
    _LEAF_NAMES,
    _LEAVES,
    _apply_typed,
    _build_leaves,
)


def _load_acli():
    """Return the in-package acli transport module (rebar_reconciler.acli)."""
    from rebar_reconciler import acli

    return acli


class HeadDriftError(Exception):
    """Raised when the tickets-branch HEAD changes mid-pass, indicating concurrent write."""


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
    "ApplyResult",
    "DirectionMismatchError",
    "EXIT_RESCHEDULE",
    "JiraAPIError",
    "RebarIdLabelWriteError",
    "RescheduleError",
    "RetryExhaustedError",
    "StatusMappingError",
    "UnknownActionError",
    "_ADF_KEY_APPLIER",
    "_AUTHORIZED_REBAR_ID_LABEL_ACTIONS",
    "_AUTHORIZED_REBAR_ID_LABEL_WRITERS",
    "_AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC",
    "_AdfModule_Applier",
    "_BRIDGE_INTERNAL_TAG_PREFIXES",
    "_ErrorsModule",
    "_JIRA_PRIORITY_MAP",
    "_JIRA_TYPE_MAP",
    "_LEAF_NAMES",
    "_LEAVES",
    "_LOCAL_STATUS_VALUES",
    "_MUTATION_KEY",
    "_MutationModule",
    "_REBAR_STATUS_LABEL_TO_LOCAL",
    "_TICKET_REDUCER_MODULE",
    "_VALID_PRIORITY_RANGE",
    "_apply_inbound_clean_label",
    "_apply_inbound_conflict",
    "_apply_inbound_create",
    "_apply_inbound_delete",
    "_apply_inbound_probe",
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
    "_get_rebar_id_guard_mode_from_config",
    "_is_illegal_transition_400",
    "_is_rebar_id_label_write_mutation",
    "_jira_key_to_local_id",
    "_jira_status_to_local",
    "_load_adf_module",
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
    client=None,
    mode=None,
    binding_store=None,
    persist: bool = True,
):
    """Polymorphic dispatch entry point.

    Two call shapes:
      1. Typed single-mutation:  apply(mutation, *, client=None) -> ApplyResult
      2. Legacy batch:            apply(mutations: list[dict], pass_id, ...) -> Path
    Selection is by argument type at the top of the function.
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
    mode, mode_mod, mutations_input, deferred_for_manifest = _partition_by_mode_cap(mode, mutations)

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

    # Create an AcliClient for inbound leaves that write back to Jira.
    if client is None and inbound_typed:
        acli_mod = _load_acli()
        client = acli_mod.AcliClient(
            jira_url=os.environ.get("JIRA_URL", ""),
            user=os.environ.get("JIRA_USER", ""),
            api_token=os.environ.get("JIRA_API_TOKEN", ""),
        )
        logger.info(
            "inbound dispatch: created AcliClient for %d inbound mutations "
            "(JIRA_URL=%s, JIRA_USER=%s)",
            len(inbound_typed),
            os.environ.get("JIRA_URL", "<unset>"),
            os.environ.get("JIRA_USER", "<unset>"),
        )

    # Deferred bug-filing directives from inbound conflict leaves, processed
    # AFTER _apply_batch to keep the apply path commit-free (bug d822).
    pending_bug_tickets: list[dict] = []

    for mut in inbound_typed:
        if not persist:
            break
        if suppression.is_suppressed(getattr(mut, "target", "")):
            continue
        result = _apply_typed(mut, client=client, repo_root=repo_root, binding_store=binding_store)
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
            )
    finally:
        if pending_bug_tickets and not is_dry_run:
            from rebar._engine import in_process_cli

            cli_path = Path(in_process_cli())
            for pending in pending_bug_tickets:
                try:
                    _file_conflict_bug_ticket(
                        cli_path,
                        pending.get("title", ""),
                        pending.get("description", ""),
                        pending.get("parent_id", ""),
                    )
                except Exception as exc:  # noqa: BLE001
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
        repo_root = Path(os.environ.get("REBAR_ROOT") or Path(__file__).resolve().parents[4])

    acli = _load_acli()
    # Mirror fetcher.fetch_snapshot's pattern: AcliClient's real constructor
    # requires (jira_url, user, api_token) — the no-arg form raises TypeError
    # on every real invocation. Read credentials from the standard
    # JIRA_URL / JIRA_USER / JIRA_API_TOKEN environment variables, defaulting
    # to "" so test/CI shims that monkey-patch _load_acli still work.
    # jira_project defaults to "DIG" (matching _attestation.py) because an empty
    # projectKey is rejected by ACLI on every CREATE — bug 4fa9-0846-519e-4c30.
    client = acli.AcliClient(
        jira_url=os.environ.get("JIRA_URL", ""),
        user=os.environ.get("JIRA_USER", ""),
        api_token=os.environ.get("JIRA_API_TOKEN", ""),
        jira_project=os.environ.get("JIRA_PROJECT", "DIG"),
    )

    rest_calls: int = 0
    deferred_creates: list[dict] = []
    mutations_with_outcomes: list[dict] = []
    events_list: list[dict] = []

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
        write_result = concurrency.rebase_retry(
            repo_root,
            lambda: _write_pass_record(repo_root, pass_id, 0),
        )
        if not write_result.ok:
            _handle_failed_write_result(write_result, pass_id)
        return manifest_path

    # Pin HEAD before first mutation
    head_pin = concurrency.snapshot_head(repo_root)

    try:
        for mutation in mutations:
            # Re-check HEAD at the start of each iteration.
            #
            # Bug f058: the tickets orphan branch is shared with the ticket
            # CLI (auto-commits via rebar create / transition / etc.)
            # and the suggestion subsystem. A parallel Claude session
            # running `rebar transition <id> closed` triggers
            # auto-compact, which commits `ticket: COMPACT <id>` to
            # tickets — that doesn't conflict with the in-flight
            # outbound mutations, but the strict-equality drift check
            # aborts the pass. Resolution: inspect the intervening
            # commit's subject. If it matches a benign external pattern
            # (ticket-CLI, suggestion, pass-lock), refresh head_pin and
            # continue. Only raise HeadDriftError when the subject
            # indicates a competing reconciler outbound write — the
            # original intent of the detector.
            current_head = concurrency.snapshot_head(repo_root)
            if current_head != head_pin:
                drift_subject = _get_commit_subject(repo_root, current_head)
                if _drift_is_benign(drift_subject):
                    # Benign external writer — accept the new HEAD and
                    # continue. Log so operators can see the writer.
                    print(  # noqa: T201
                        f"tolerated_drift: {head_pin[:8]}→{current_head[:8]} "
                        f"subject={drift_subject!r}",
                        file=sys.stderr,
                    )
                    head_pin = current_head
                else:
                    raise HeadDriftError(
                        f"drift: {head_pin[:8]}→{current_head[:8]} subject={drift_subject!r}"
                    )

            action = mutation.get("action", "")
            outcome = dict(mutation)

            # Audit pass: extend the rebar-id label write guard to the legacy
            # batch dispatch path. create_one/update_one/delete_one all issue
            # outbound Jira writes, so each batch mutation maps to an
            # outbound_<action> leaf for guard-name purposes. Without this
            # call, _audit_rebar_id_label_writes was bypassed for every legacy
            # dict-shaped mutation — only _apply_typed enforced the contract.
            _audit_rebar_id_label_writes(f"outbound_{action}", [_BatchAuditView(mutation)])

            if action == "create":
                # Bug ea6d-e4b2-a316-45ec: collect any add_comment failures so a
                # swallowed comment sub-mutation during an outbound CREATE surfaces
                # in the batch outcome rather than reporting a clean error=None,
                # mirroring the update-path handling below (bug 6afc).
                _comment_errors: list[str] = []
                result = create_one(
                    mutation,
                    client,
                    rest_calls=rest_calls,
                    deferred_creates=deferred_creates,
                    events_list=events_list,
                    repo_root=repo_root,
                    binding_store=binding_store,
                    comment_errors=_comment_errors,
                )
                # Only count REST call on actual create (not dedup-skipped, not deferred)
                if result is not None and result.get("status") != "dedup-create-skipped":
                    rest_calls += 1
                outcome["result"] = result
                # Surface swallowed comment failures. NON-fatal — the issue create
                # above genuinely succeeded — so we record them in a dedicated
                # field rather than overwriting outcome["error"], mirroring the
                # update-path soft-fail style.
                if _comment_errors:
                    outcome["comment_errors"] = list(_comment_errors)
            elif action == "update":
                # Bug 17b5-dda4-6662-4616: AssigneeNotFoundError (raised by
                # client.update_issue's Phase A pre-validation when the
                # local assignee doesn't map to a real Jira account, e.g.
                # 'Worktree' git-config default) was killing the entire
                # batch because the surrounding try-block only handles
                # HeadDriftError. Soft-fail this mutation: record an
                # alert, mark outcome error, and continue with the rest.
                # Mirrors the existing 400-illegal-transition fallback
                # in update_one and the BRIDGE_ALERT pattern in create_one.
                # Bug 6afc-20ee-84e5-4dd5: collect any add_comment failures so a
                # swallowed comment sub-mutation surfaces in the batch outcome
                # rather than reporting a clean error=None.
                _comment_errors: list[str] = []
                try:
                    result = update_one(mutation, client, comment_errors=_comment_errors)
                except urllib.error.HTTPError as exc:
                    # Bug tan-coin-atone (6614-43cd-3a48-4f63): an outbound
                    # update against a DELETED Jira issue (stale binding, 1e08
                    # class) routes status/priority through REST sub-calls
                    # (transition_issue / update_priority) that raise a RAW
                    # urllib.error.HTTPError 404 — NOT a JiraAPIError — so the
                    # update_one comment-fallback try/except (which only handles
                    # JiraAPIError) misses it and the 404 escapes reconcile_once,
                    # aborting the whole pass (GHA run 27023829257). A 404 on a
                    # single mutation's target means the issue is gone: this is a
                    # PER-MUTATION failure, never pass-fatal. Soft-fail ONLY 404 —
                    # other HTTP errors (e.g. 5xx) keep current behavior and
                    # propagate (matching delete_one's already-gone tolerance and
                    # the AssigneeNotFoundError soft-fail below). Positive-404
                    # evidence feeds the binding-GC design in
                    # docs/designs/sync-hardening-proposal.md Item 4b.
                    if exc.code != 404:
                        raise
                    _outcome_key = mutation.get("key") or mutation.get("local_id") or "<unknown>"
                    logger.warning(
                        "outbound update skipped: Jira issue %s gone (HTTP 404) "
                        "— stale binding (1e08); recording per-mutation failure "
                        "and continuing the pass",
                        _outcome_key,
                    )
                    outcome["result"] = None
                    outcome["error"] = f"stale-binding-404: {exc!s}"
                    mutations_with_outcomes.append(outcome)
                    # Per-mutation RECON line matches the regular path.
                    print(  # noqa: T201
                        f"RECON: batch_outcome action={action} "
                        f"key={_outcome_key} "
                        f"error={outcome['error']!r}",
                        file=sys.stderr,
                    )
                    continue
                except acli.AssigneeNotFoundError as exc:
                    alert_store = _load_alert_store()
                    alert_store.append(
                        {
                            "kind": "outbound-update-assignee-unresolved",
                            "key": mutation.get("key"),
                            "local_id": mutation.get("local_id"),
                            "assignee": ((mutation.get("fields") or {}).get("assignee")),
                            "pass_id": pass_id,
                            "timestamp_ns": time.time_ns(),
                            "reason": str(exc),
                        },
                        repo_root=repo_root,
                    )
                    outcome["result"] = None
                    outcome["error"] = f"assignee-unresolved: {exc!s}"
                    mutations_with_outcomes.append(outcome)
                    # Per-mutation RECON line matches the regular path.
                    _outcome_key = mutation.get("key") or mutation.get("local_id") or "<unknown>"
                    print(  # noqa: T201
                        f"RECON: batch_outcome action={action} "
                        f"key={_outcome_key} "
                        f"error={outcome['error']!r}",
                        file=sys.stderr,
                    )
                    continue
                outcome["result"] = result
                # Bug 6afc-20ee-84e5-4dd5: surface swallowed comment failures.
                # NON-fatal — the scalar update above genuinely succeeded — so we
                # record them in a dedicated field rather than overwriting
                # outcome["error"], mirroring the soft-fail style of the
                # stale-binding-404 / assignee-unresolved handlers.
                if _comment_errors:
                    outcome["comment_errors"] = list(_comment_errors)
                # Persist provenance for set-valued fields after update
                jira_key = mutation.get("key", "")
                if jira_key:
                    conflict_resolver = _load_conflict_resolver()
                    mapping_path = repo_root / "bridge_state" / "mapping.json"
                    for field_name, field_value in mutation.get("fields", {}).items():
                        if conflict_resolver.FIELD_CLASSES.get(field_name) == "set":
                            _persist_field_provenance(
                                mapping_path, jira_key, field_name, field_value
                            )
            elif action == "delete":
                delete_one(mutation, client)
                outcome["result"] = None
            else:
                outcome["result"] = None
                outcome["error"] = f"unknown action: {action!r}"

            mutations_with_outcomes.append(outcome)
            # Bug b859 (Part 0c): per-mutation RECON line so operators see
            # which dispatch actually ran without parsing the manifest.
            # Targets the legacy batch path (the dominant outbound CREATE +
            # UPDATE channel today). Truncated to single-line; full
            # mutation lives in the manifest for forensic dives.
            _outcome_key = mutation.get("key") or mutation.get("local_id") or "<unknown>"
            _outcome_err = outcome.get("error")
            print(  # noqa: T201
                f"RECON: batch_outcome action={action} key={_outcome_key} error={_outcome_err!r}",
                file=sys.stderr,
            )

    except HeadDriftError:
        # Emit abort event as structured log and re-raise for the caller
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

    manifest = {
        "pass_id": pass_id,
        "mutation_count": len(mutations),
        "mutations": mutations_with_outcomes,
        "events": events_list,
    }

    snapshots_dir = repo_root / "bridge_state" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = snapshots_dir / f"{pass_id}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Wrap the tickets-branch write in rebase_retry (up to 3 attempts).
    # On non-fast-forward push rejection the helper fetches + rebases + retries.
    # On exhaustion, emit a health event to stderr and raise RescheduleError so
    # the process can exit with EXIT_RESCHEDULE.  No retry-counter file is
    # written to disk; the next pass starts fresh.
    write_result = concurrency.rebase_retry(
        repo_root,
        lambda: _write_pass_record(repo_root, pass_id, len(mutations)),
    )
    if not write_result.ok:
        _handle_failed_write_result(write_result, pass_id)

    return manifest_path
