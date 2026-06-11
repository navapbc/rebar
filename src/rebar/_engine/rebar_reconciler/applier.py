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

import contextlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
    ApplyResult,
    DirectionMismatchError,
    RebarIdLabelWriteError,
    StatusMappingError,
    UnknownActionError,
    _ErrorsModule,
    _MUTATION_KEY,
    _MutationModule,
    _direction_guard,
    _errors_module,
    _load_errors_module,
    _load_mutation_module,
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
    _AdfModule_Applier,
    _BRIDGE_INTERNAL_TAG_PREFIXES,
    _EVENT_APPEND_MODULE,
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
    _load_adf_module,
    _load_event_append,
    _load_ticket_reducer,
    _normalize_adf_body,
    _read_latest_status,
    _resolve_priority,
    _resolve_tracker_dir,
    _write_event_file,
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
def _file_conflict_bug_ticket(
    cli_path: Path, title: str, description: str, parent_id: str
) -> str:
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
        res = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if res.returncode != 0:
        return ""
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# The typed-dispatch routing table + dispatcher live in typed_dispatch.py.
# Re-exported so apply() (resident) + test_leaves_registry_coverage resolve.
from rebar_reconciler.typed_dispatch import (  # noqa: E402
    _LEAF_NAMES,
    _LEAVES,
    _apply_typed,
    _build_leaves,
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
    _BatchAuditView,
    _audit_rebar_id_label_writes,
    _get_rebar_id_guard_mode_from_config,
    _is_rebar_id_label_write_mutation,
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
def _load_acli():
    """Load acli-integration module via importlib."""
    acli_path = Path(__file__).parent.parent / "acli-integration.py"
    spec = importlib.util.spec_from_file_location("acli_integration", acli_path)
    if spec is None:
        raise FileNotFoundError(f"acli-integration.py not found at {acli_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("acli_integration", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


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


def _load_mode_module():
    """Lazy-load mode.py under a stable key so MODE_CAPS / Mode are accessible.

    Uses the SAME dotted key as __main__._MODE_KEY so a single module object
    is shared with the entry-point loader; tests that pre-seed sys.modules
    under that key see their stub here too.
    """
    key = "rebar_reconciler.mode"
    if key in sys.modules:
        return sys.modules[key]
    mode_path = Path(__file__).parent / "mode.py"
    spec = importlib.util.spec_from_file_location(key, mode_path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"mode.py not found at {mode_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_manifest_renderer():
    """Lazy-load manifest_renderer.py."""
    key = "rebar_reconciler.manifest_renderer"
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).parent / "manifest_renderer.py"
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"manifest_renderer.py not found at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _mode_sort_key(m) -> tuple[str, str, str]:
    """Deterministic ordering key for cap enforcement.

    Outbound creates sort first (priority "0") so they land within the
    bootstrap-strict cap window. Without this, 'inbound' < 'outbound'
    lexicographically causes all cap slots to go to inbound mutations,
    deferring outbound creates indefinitely (bug d5a2-3fc8).
    """
    d = getattr(m, "direction", None)
    a = getattr(m, "action", None)
    t = getattr(m, "target", None)
    if isinstance(m, dict):
        d = d if d is not None else m.get("direction", "")
        a = a if a is not None else m.get("action", "")
        t = t if t is not None else (m.get("key", "") or m.get("target", ""))
    d_str = str(getattr(d, "value", d) or "")
    a_str = str(getattr(a, "value", a) or "")
    if d_str == "outbound" and a_str == "create":
        d_str = "0_outbound_create"
    return (d_str, a_str, str(t or ""))


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
         When the first positional argument is a Mutation instance, dispatch
         via _LEAVES. Raises UnknownActionError for unregistered pairs (with
         zero side-effects) and DirectionMismatchError if a leaf is invoked
         with a mismatched direction.
      2. Legacy batch:            apply(mutations: list[dict], pass_id, ...) -> Path
         Original manifest-writing batch dispatcher; behavior unchanged.

    Selection is by argument type at the top of the function.
    """
    # Typed-mutation dispatch path: first arg is a Mutation instance.
    # Duck-type rather than isinstance() because mutation.py may be loaded
    # under different module names depending on how the importing test rig
    # set up sys.modules — a strict isinstance() check would silently fall
    # through to the legacy batch path and raise a confusing TypeError.
    mut_mod = _load_mutation_module()
    if isinstance(mutations, mut_mod.Mutation) or (
        type(mutations).__name__ == "Mutation"
        and hasattr(mutations, "direction")
        and hasattr(mutations, "action")
    ):
        return _apply_typed(
            mutations, client=client, repo_root=repo_root, binding_store=binding_store
        )

    # Legacy batch path requires pass_id.
    if pass_id is None:
        raise TypeError(
            "apply() legacy batch form requires pass_id as the second argument"
        )

    # -------------------------------------------------------------------------
    # Mode-cap enforcement (story 286b).
    #
    # When *mode* is provided, look up the per-mode cap in MODE_CAPS and
    # partition the incoming mutations into (applied, deferred). The applied
    # list is what the direction-aware dispatch loop below actually executes;
    # the deferred list is reported via the mode-specific manifest renderer.
    #
    # Cap semantics:
    #   - cap is None    → uncapped (LIVE): apply all; manifest renderer is
    #                      NOT invoked (LIVE writes no manifest file).
    #   - cap == 0       → DRY_RUN: apply NOTHING (no leaf invoked, no batch
    #                      iteration); manifest still written listing every
    #                      mutation as deferred.
    #   - cap > 0        → BOOTSTRAP_STRICT (10) / BOOTSTRAP_THROTTLE (100):
    #                      sort by (direction, action, target), apply first
    #                      `cap`, defer the rest.
    #
    # When *mode* is None (the call shape used by legacy callers that have not
    # yet been migrated), behaviour is unchanged from before: apply everything,
    # write the legacy flat manifest. This preserves the contract for the wide
    # surface of existing tests under tests/unit/rebar_reconciler/.
    # -------------------------------------------------------------------------
    mutations_input = list(mutations or [])
    deferred_for_manifest: list = []
    # Hoist the mode module load to a single call per apply() invocation.
    # Previously _load_mode_module() was called at three sites (cap lookup,
    # DRY_RUN dispatch skip, manifest renderer dispatch); collapsing to one
    # avoids redundant importlib work and a class-identity hazard if the
    # module ever ends up loaded under multiple sys.modules keys mid-call.
    mode_mod = _load_mode_module() if mode is not None else None
    if mode is not None:
        # Validate / coerce mode to a Mode enum member (findings #1/#2).
        # Accepting raw strings would let MODE_CAPS.get() return None for
        # unrecognised values, silently triggering the uncapped LIVE path.
        if isinstance(mode, str):
            mode = mode_mod.Mode.from_str(mode)
        if not isinstance(mode, mode_mod.Mode):
            raise TypeError(
                f"mode must be a Mode enum member or a recognised mode string, "
                f"got {type(mode).__name__}: {mode!r}"
            )
        cap = mode_mod.MODE_CAPS.get(mode)
        # Sort deterministically before applying the cap so the applied /
        # deferred partition is reproducible across passes.
        ordered = sorted(mutations_input, key=_mode_sort_key)
        if cap is None:
            # LIVE: uncapped — proceed with all mutations through the normal
            # dispatch path below. Manifest renderer is skipped post-apply.
            mutations_input = ordered
        elif cap == 0:
            # DRY_RUN: skip the apply loop entirely. Every mutation is deferred.
            deferred_for_manifest = ordered
            mutations_input = []
        else:
            # BOOTSTRAP_STRICT / BOOTSTRAP_THROTTLE: cap then defer remainder.
            mutations_input = ordered[:cap]
            deferred_for_manifest = ordered[cap:]

    # Direction-aware dispatch (defect #8): partition typed Mutations by
    # direction. Inbound Mutations route through _apply_typed per-mutation
    # (so each one fires the inbound leaf from _LEAVES against the local
    # tracker). Outbound Mutations are normalized to dicts and pass through
    # _apply_batch (legacy manifest-writing path). Untyped dict entries
    # default to the outbound batch path — that is the legacy contract.
    #
    # Previously this code path raised TypeError as a fail-closed guard
    # against inbound traffic. The guard was correct in intent — routing
    # inbound through _apply_batch would execute Jira-side outbound
    # handlers — but the production path produces overwhelmingly inbound
    # Mutations on first run (empty local mirror), so the guard blocked
    # every pass. The actual fix is to route inbound through the existing
    # _apply_typed handler (which already covers all (inbound, *) pairs in
    # _LEAVES).
    mutations_list = list(mutations_input)

    def _looks_like_mutation(m) -> bool:
        if isinstance(m, mut_mod.Mutation):
            return True
        return (
            type(m).__name__ == "Mutation"
            and hasattr(m, "direction")
            and hasattr(m, "action")
        )

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

    # Inbound: per-mutation dispatch via _apply_typed. Order preserved from
    # the source list so observable behaviour is deterministic.
    #
    # suppress_pair follow-on contract (story bd19-d744-b8c7-4079): when a
    # leaf returns a payload with follow_on={'kind': 'suppress_pair',
    # 'local_id': X, 'jira_key': Y}, all subsequent inbound mutations
    # targeting either X or Y AND all outbound batch entries targeting Y are
    # dropped from this pass so the conflict signal is not stomped by stale
    # follow-up mutations.
    # Suppress-pair index: O(1) lookup. We maintain two sets of canonical
    # identifiers (jira-keys-as-given and local_ids) plus a set of computed
    # local-id forms (jira_key → _jira_key_to_local_id) so the third match-
    # arm (computed-form: target=='DIG-7' suppresses subsequent
    # target=='jira-dig-7') is also O(1). Replaces the prior O(n²) list
    # scan flagged in PR #375 review thread 3306949610.
    suppressed_targets: set[str] = set()
    suppressed_pairs: set[tuple[str, str]] = set()

    def _is_suppressed(target: str) -> bool:
        if not target:
            return False
        return target in suppressed_targets

    def _record_suppression(local_id: str, jira_key: str) -> None:
        suppressed_pairs.add((local_id, jira_key))
        if jira_key:
            suppressed_targets.add(jira_key)
            # Computed-form: a later mutation targeting the local-id form of
            # this jira_key (e.g. 'jira-dig-7' after suppressing 'DIG-7')
            # must also be dropped.
            suppressed_targets.add(_jira_key_to_local_id(jira_key))
        if local_id:
            suppressed_targets.add(local_id)

    # Create an AcliClient for inbound leaves that need to write back to
    # Jira (rebar-id label + local_id property). The caller (reconcile_once)
    # does not pass a client — the fetcher creates its own for reading, and
    # the legacy batch path (_apply_batch) creates its own for outbound writes.
    # The inbound dispatch path needs its own for the write-back step.
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

    # Collect deferred bug-filing directives from inbound conflict leaves.
    # These are processed AFTER _apply_batch returns to keep the apply path
    # commit-free (bug d822 — the bug-filing CLI commits to the tickets
    # branch, which would advance HEAD inside _apply_batch's drift-guarded
    # loop and raise spurious HeadDriftError).
    pending_bug_tickets: list[dict] = []

    for mut in inbound_typed:
        # No-write (cap-0) modes must not APPLY: the inbound leaves write local
        # CREATE events + Jira-side labels/properties. cap-0 already empties
        # mutations_input upstream so this loop is a no-op today, but gate it
        # explicitly so the no-write contract is self-enforcing rather than
        # relying on that coupling (review M1).
        if not persist:
            break
        if _is_suppressed(getattr(mut, "target", "")):
            continue
        result = _apply_typed(
            mut, client=client, repo_root=repo_root, binding_store=binding_store
        )
        result_payload = (
            getattr(result, "payload", None) if result is not None else None
        )
        follow_on = (
            result_payload.get("follow_on")
            if isinstance(result_payload, dict)
            else None
        )
        if isinstance(follow_on, dict) and follow_on.get("kind") == "suppress_pair":
            _record_suppression(
                follow_on.get("local_id", ""), follow_on.get("jira_key", "")
            )
        pending = (
            result_payload.get("pending_bug_ticket")
            if isinstance(result_payload, dict)
            else None
        )
        if isinstance(pending, dict):
            pending_bug_tickets.append(pending)

    # Bug b859 (Part 0c): structured RECON line after inbound typed dispatch
    # so operators see how many inbound mutations actually ran (vs were
    # suppressed). Independent of the manifest tally because suppression
    # decisions live only in this loop scope.
    print(  # noqa: T201
        f"RECON: typed_inbound_dispatched count={len(inbound_typed)} "
        f"suppressed_pairs={len(suppressed_pairs)}",
        file=sys.stderr,
    )

    # Outbound (or untyped dict): normalize typed Mutations to dicts so
    # _apply_batch can iterate, then route through the legacy batch path.
    # _apply_batch handles an empty list cleanly (writes an empty manifest)
    # so the all-inbound case still produces a manifest path for the caller.
    outbound_list = [
        _mutation_to_batch_dict(m) if _looks_like_mutation(m) else m
        for m in outbound_or_untyped
    ]
    # Drop any outbound entries whose key matches a suppressed pair.
    if suppressed_pairs:
        outbound_list = [
            d for d in outbound_list if not _is_suppressed(d.get("key", ""))
        ]
    # In DRY_RUN, skip the legacy batch dispatcher entirely so the test
    # contract ("neither _apply_typed nor _apply_batch is invoked") holds.
    # The renderer block below writes the asymmetric manifest from scratch.
    #
    # Wrap _apply_batch in try/finally so deferred bug-filing runs even when
    # _apply_batch raises (HeadDriftError, RescheduleError, etc.). Without
    # this guarantee, an apply-batch exception unwinds apply() and the
    # collected pending_bug_ticket directives are silently dropped — losing
    # the operator's audit trail for conflicts that were already suppressed
    # by the leaf's follow_on emission. The deferred-filing block runs
    # outside the drift-guarded loop so its own commits cannot re-trigger
    # the drift detector.
    is_dry_run = mode_mod is not None and mode == mode_mod.Mode.DRY_RUN
    manifest_path = None
    try:
        # When persist is False (cap-0 no-write modes), skip _apply_batch
        # entirely so no manifest file (not even an empty one) is written.
        # cap-0 already left mutations_input == [] so the batch would be a
        # no-op write anyway; this just suppresses the file side effect.
        if not is_dry_run and persist:
            manifest_path = _apply_batch(
                outbound_list,
                pass_id,
                repo_root=repo_root,
                binding_store=binding_store,
            )
    finally:
        # Deferred bug-filing for inbound conflicts (bug d822). Skipped in
        # DRY_RUN — that mode must not produce any side effects, and
        # pending_bug_tickets is always empty there (inbound dispatch loop
        # runs over an empty list under DRY_RUN). The is_dry_run guard is
        # defense-in-depth.
        if pending_bug_tickets and not is_dry_run:
            cli_path = Path(
                os.environ.get("REBAR_TICKET_CLI")
                or (Path(__file__).resolve().parent.parent / "rebar")
            )
            for pending in pending_bug_tickets:
                try:
                    _file_conflict_bug_ticket(
                        cli_path,
                        pending.get("title", ""),
                        pending.get("description", ""),
                        pending.get("parent_id", ""),
                    )
                except Exception as exc:  # noqa: BLE001
                    # Bug-filing failure is non-fatal — the conflict is
                    # still suppressed via the follow_on; only the audit
                    # ticket is lost. Per-iteration except prevents one
                    # failed filing from blocking the others.
                    print(  # noqa: T201
                        f"deferred_bug_filing_failed: "
                        f"local_id={pending.get('local_id')!r} "
                        f"jira_key={pending.get('jira_key')!r} err={exc!r}",
                        file=sys.stderr,
                    )

    # -------------------------------------------------------------------------
    # Mode-specific manifest emission (story 286b).
    #
    # When *mode* is provided, replace the flat legacy manifest with the
    # asymmetric shape dispatched by manifest_renderer:
    #
    #   - DRY_RUN / BOOTSTRAP_STRICT  → render_dry_run_or_strict
    #   - BOOTSTRAP_THROTTLE          → render_throttle
    #   - LIVE                        → no manifest file; remove the legacy
    #                                    write and return None
    #
    # The legacy manifest written by _apply_batch is left in place when
    # mode is None (legacy callers depend on it). Otherwise we overwrite or
    # remove it as required by the mode contract.
    # -------------------------------------------------------------------------
    if mode_mod is not None:
        renderer_mod = _load_manifest_renderer()
        applied_for_manifest = list(mutations_list)

        if mode == mode_mod.Mode.LIVE:
            # LIVE: no manifest file per contract. Remove the legacy manifest
            # written by _apply_batch.
            try:
                if manifest_path is not None and Path(manifest_path).exists():
                    Path(manifest_path).unlink()
            except OSError:
                pass
            return None

        if mode == mode_mod.Mode.BOOTSTRAP_THROTTLE:
            rendered = renderer_mod.render_throttle(
                applied_for_manifest, deferred_for_manifest
            )
        else:
            # DRY_RUN and BOOTSTRAP_STRICT share the same renderer.
            rendered = renderer_mod.render_dry_run_or_strict(
                applied_for_manifest, deferred_for_manifest
            )

        rendered_with_meta = {
            "pass_id": pass_id,
            "mode": getattr(mode, "value", str(mode)),
            "applied_count": rendered.get("applied_count", len(applied_for_manifest)),
            "deferred_count": rendered.get(
                "deferred_count", len(deferred_for_manifest)
            ),
            "outbound": rendered.get("outbound"),
            "inbound": rendered.get("inbound"),
        }
        if "spot_check" in rendered:
            rendered_with_meta["spot_check"] = rendered["spot_check"]
        # Also expose the deferred mutations list (sorted) so tests and
        # operators can audit exactly what was held back.
        rendered_with_meta["deferred"] = [
            {
                "direction": str(
                    getattr(getattr(m, "direction", ""), "value", "")
                    or (m.get("direction", "") if isinstance(m, dict) else "")
                ),
                "action": str(
                    getattr(getattr(m, "action", ""), "value", "")
                    or (m.get("action", "") if isinstance(m, dict) else "")
                ),
                "target": _mode_sort_key(m)[2],
            }
            for m in deferred_for_manifest
        ]

        # No-write contract (cap-0 modes, persist=False): produce the full
        # computed plan as a dict and RETURN it WITHOUT writing any manifest
        # file. The caller (reconcile_once) surfaces this plan to stdout and
        # treats manifest_path as None for tally purposes.
        if not persist:
            return rendered_with_meta

        # DRY_RUN may have skipped _apply_batch entirely (when mutations_input
        # was empty) — _apply_batch still wrote an empty manifest. Either way,
        # the manifest_path is valid; overwrite with the asymmetric shape.
        if manifest_path is None:
            if repo_root is None:
                repo_root_resolved = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])
            else:
                repo_root_resolved = repo_root
            snapshots_dir = repo_root_resolved / "bridge_state" / "snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = snapshots_dir / f"{pass_id}.manifest.json"
        # Atomic write via tempfile + os.replace to avoid race conditions
        # when concurrent DRY_RUN passes share the same pass_id (finding #3).
        manifest_dir = Path(manifest_path).parent
        manifest_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=manifest_dir,
            prefix=f"{pass_id}.",
            suffix=".json.tmp",
        )
        try:
            with os.fdopen(fd, "w") as tmp_f:
                json.dump(rendered_with_meta, tmp_f, indent=2)
            os.replace(tmp_path, str(manifest_path))
        except BaseException:
            # Clean up the temp file on any failure.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

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
        repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])

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
                        f"drift: {head_pin[:8]}→{current_head[:8]} "
                        f"subject={drift_subject!r}"
                    )

            action = mutation.get("action", "")
            outcome = dict(mutation)

            # Audit pass: extend the rebar-id label write guard to the legacy
            # batch dispatch path. create_one/update_one/delete_one all issue
            # outbound Jira writes, so each batch mutation maps to an
            # outbound_<action> leaf for guard-name purposes. Without this
            # call, _audit_rebar_id_label_writes was bypassed for every legacy
            # dict-shaped mutation — only _apply_typed enforced the contract.
            _audit_rebar_id_label_writes(
                f"outbound_{action}", [_BatchAuditView(mutation)]
            )

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
                if (
                    result is not None
                    and result.get("status") != "dedup-create-skipped"
                ):
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
                    result = update_one(
                        mutation, client, comment_errors=_comment_errors
                    )
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
                    _outcome_key = (
                        mutation.get("key") or mutation.get("local_id") or "<unknown>"
                    )
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
                            "assignee": (
                                (mutation.get("fields") or {}).get("assignee")
                            ),
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
                    _outcome_key = (
                        mutation.get("key") or mutation.get("local_id") or "<unknown>"
                    )
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
            _outcome_key = (
                mutation.get("key") or mutation.get("local_id") or "<unknown>"
            )
            _outcome_err = outcome.get("error")
            print(  # noqa: T201
                f"RECON: batch_outcome action={action} key={_outcome_key} "
                f"error={_outcome_err!r}",
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
