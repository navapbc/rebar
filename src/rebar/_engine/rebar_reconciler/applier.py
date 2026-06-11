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

# Loop-breaker marker (mirrors inbound_differ.RECONCILER_MARKER).
# Outbound comments embed this token so inbound passes — including the
# bootstrap in _apply_inbound_create — can skip our own echoes.
_RECONCILER_MARKER_APPLIER = "<!-- rebar:reconciler-echo -->"

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
_MutationModule = (
    None  # late-loaded mutation module; written by _load_mutation_module()
)
_ErrorsModule = None  # late-loaded _errors module; written by _load_errors_module()


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Result of a typed-mutation apply() dispatch.

    direction/action mirror the Mutation that was dispatched, so callers can
    confirm which leaf executed without re-reading the input. payload carries
    any leaf-specific return data (empty dict by default for the stub leaves).
    """

    direction: Any
    action: Any
    payload: dict[str, Any]


_MUTATION_KEY = "rebar_reconciler.mutation"


def _load_mutation_module():
    """Lazy-load the mutation module under the canonical dotted sys.modules key.

    Uses the SAME key (``rebar_reconciler.mutation``) as
    invariants.py and differ.py so ``Mutation`` / ``MutationDirection`` /
    ``MutationAction`` retain a single class identity across the reconciler.
    Previously each caller loaded under its own private key, producing distinct
    class objects per module — ``isinstance`` and ``is`` comparisons silently
    crossed boundaries and routed mutations to the wrong leaf.
    """
    global _MutationModule
    if _MutationModule is not None:
        return _MutationModule
    if _MUTATION_KEY in sys.modules:
        _MutationModule = sys.modules[_MUTATION_KEY]
        return _MutationModule
    mut_path = Path(__file__).parent / "mutation.py"
    spec = importlib.util.spec_from_file_location(_MUTATION_KEY, mut_path)
    if spec is None:
        raise FileNotFoundError(f"mutation.py not found at {mut_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MUTATION_KEY] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _MutationModule = mod
    return mod


def _load_errors_module():
    """Lazy-load _errors module."""
    global _ErrorsModule
    if _ErrorsModule is not None:
        return _ErrorsModule
    err_path = Path(__file__).parent / "_errors.py"
    spec = importlib.util.spec_from_file_location("rebar_reconciler_errors", err_path)
    if spec is None:
        raise FileNotFoundError(f"_errors.py not found at {err_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("rebar_reconciler_errors", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _ErrorsModule = mod
    return mod


# Re-export error classes so callers can import them from applier.py.
# Internal uses still go through _load_errors_module() to preserve lazy-load
# semantics; these module-level names exist for the public import surface.
_errors_module = _load_errors_module()
StatusMappingError = _errors_module.StatusMappingError
DirectionMismatchError = _errors_module.DirectionMismatchError
UnknownActionError = _errors_module.UnknownActionError
RebarIdLabelWriteError = _errors_module.RebarIdLabelWriteError


# Subject prefixes considered "benign" for HEAD-drift tolerance — i.e.,
# external writers that don't conflict with in-flight outbound mutations.
# Bug f058: parallel Claude sessions running `rebar transition` /
# `rebar create` / etc. emit `ticket: <VERB>` commits to the tickets
# branch during a reconciler pass. The suggestion subsystem emits
# `suggestion: RECORD`. Other reconciler passes emit `acquire lock` /
# `release lock`. Competing outbound writes emit `pass_record: <pass_id>`
# — the original concern the drift detector was built for — and remain
# non-benign.
_BENIGN_DRIFT_PREFIXES: tuple[str, ...] = (
    "ticket:",
    "suggestion:",
    "acquire lock",
    "release lock",
)


def _drift_is_benign(subject: str) -> bool:
    """Return True if a commit subject indicates a benign external writer.

    Used by ``_apply_batch``'s drift detector to distinguish ticket-CLI
    auto-commits and pass-lock metadata from competing reconciler outbound
    writes. Benign drift refreshes ``head_pin`` and continues; non-benign
    drift raises HeadDriftError as before.
    """
    if not subject:
        return False
    return any(subject.startswith(p) for p in _BENIGN_DRIFT_PREFIXES)


def _get_commit_subject(repo_root, commit_sha: str) -> str:
    """Return the subject line of *commit_sha* in repo_root, or "" on error.

    Failures here are non-fatal — when we can't read the subject, the
    caller treats the drift as non-benign (fail-closed) and raises
    HeadDriftError as the strict detector originally did.
    """
    import subprocess as _sp

    if not commit_sha:
        return ""
    try:
        result = _sp.run(
            ["git", "-C", str(repo_root), "log", "-1", commit_sha, "--format=%s"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, _sp.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _direction_guard(mutation, expected_direction) -> None:
    """Defense-in-depth: assert mutation.direction matches the leaf's declared
    direction. In normal flow _LEAVES lookup already routes correctly; this
    raises DirectionMismatchError if a leaf is invoked directly with the wrong
    direction (e.g. via the test harness bypassing _LEAVES).

    Compare by string value rather than identity. The reconciler loads
    mutation.py multiple times via importlib (once per importing module), and
    each load creates a distinct MutationDirection enum class. Two enum
    members with the same value but from different class instances are NOT
    identity-equal, so ``is not`` would fire spuriously on filtered passes
    where a Mutation built under one module load reaches a leaf imported
    under another.
    """
    expected_val = expected_direction.value
    actual_val = getattr(mutation.direction, "value", mutation.direction)
    if expected_val != actual_val:
        errs = _load_errors_module()
        raise errs.DirectionMismatchError(
            f"leaf expects direction={expected_val!s}, got direction={actual_val!s}"
        )


# ---------------------------------------------------------------------------
# Per-leaf stub handlers.
#
# Each leaf:
#   1. Calls _direction_guard() with its own declared direction (defense-in-depth).
#   2. Performs the leaf-specific side effect (currently stubbed — real ACLI
#      wiring lands in a follow-on task).
#   3. Returns an ApplyResult.
# ---------------------------------------------------------------------------


def _apply_outbound_create(mutation, *, client=None, repo_root=None) -> ApplyResult:
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.outbound)
    if client is None:
        # Stub path: preserved for tests that don't exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})
    payload = dict(mutation.payload)
    try:
        _call_with_retry(client.create_issue, payload)
    except Exception:
        # Rollback path: if a Jira issue was (likely) created before the failure
        # surfaced, delete it via the same retry helper so transient delete
        # failures are also retried. Swallow any rollback error so the ORIGINAL
        # create exception is what re-raises to the caller.
        key = payload.get("key_hint") or mutation.target
        try:
            _call_with_retry(client.delete_issue, key)
        except Exception:  # noqa: BLE001
            # Best-effort rollback: swallow delete errors so the original
            # create exception propagates to the caller unchanged.
            pass
        raise
    return ApplyResult(mutation.direction, mutation.action, {})


# Allowlist of fields that can be pushed outbound via update_issue. Other
# fields in the changed_fields set are silently dropped — pushing arbitrary
# fields outbound is a higher-blast-radius change that lands in a follow-up
# story. Status is governed separately by REBAR_RECONCILER_STATUS_GATING.
# "parent" is intentionally included but routed to client.set_parent
# (REST PUT /rest/api/3/issue/{key} {"fields":{"parent":{"key":K}}}) rather
# than client.update_issue — ACLI edit does not support reparenting
# (ticket 8b25-ae7a-efc3-47f6).
_OUTBOUND_UPDATE_ALLOWLIST = frozenset(
    {"summary", "description", "assignee", "priority", "status", "parent"}
)


def _route_status_via_draft5(mutation, *, client=None):
    """Stub for status routing via draft5 protocol.

    The final implementation of outbound status push (transition mapping,
    workflow-state lookup, etc.) lands in a later epic. v1 just acknowledges
    the dispatch so the gating contract is exercised end-to-end.
    """
    # Intentionally a no-op stub. Real impl arrives with the status-push story.
    return None


def _apply_outbound_update(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """v1 outbound update — push allowlisted fields, labels, and comments.

    Behavior:
      - Reads ``mutation.payload['changed_fields']`` (falls back to
        ``mutation.payload`` itself for callers that pass a flat dict).
      - Filters the field set to ``_OUTBOUND_UPDATE_ALLOWLIST``; non-allowlisted
        fields are silently dropped (no side-effects on those fields).
      - Pushes the allowlisted fields via ``client.update_issue``
        using the F3-pinned ``update_issue(jira_key, **fields)`` signature,
        routed through ``_call_with_retry``.
      - Dispatches ``payload['labels']`` (list of {action, label} dicts) via
        ``client.add_label`` / ``client.remove_label``, matching update_one.
        Label failures are logged but non-fatal (scalar update already succeeded).
      - Dispatches ``payload['comments']`` (list of {body} dicts) via
        ``client.add_comment``, matching update_one. Comment failures are logged
        but non-fatal.
      - Emits a WARNING when the effective work set (allowed fields + labels +
        comments) is empty — prevents a silent no-op masquerading as success
        when the mutation carries only non-allowlisted fields.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.outbound)

    if client is None:
        # Stub path: preserved for tests that don't exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})

    payload = dict(mutation.payload or {})
    changed_fields = payload.get("changed_fields")
    if changed_fields is None:
        changed_fields = payload

    # Bug 85a1 (Gap 8): status outbound is now first-class. The previous
    # REBAR_RECONCILER_STATUS_GATING gate has been removed — status flows
    # through ``client.update_issue`` which routes status to
    # ``transition_issue`` (REST POST /transitions). The legacy
    # ``_route_status_via_draft5`` no-op stub is unused.
    # status stays in changed_fields and is forwarded below.

    # Filter to allowlist. Non-allowlisted fields are silently dropped.
    # "parent" is extracted before forwarding to update_issue — ACLI edit
    # does not support reparenting; route via client.set_parent (REST PUT).
    allowed = {
        k: v for k, v in changed_fields.items() if k in _OUTBOUND_UPDATE_ALLOWLIST
    }
    # Route parent reparent via client.set_parent (ticket 8b25).
    parent_key = allowed.pop("parent", None)
    if parent_key is not None:
        try:
            _call_with_retry(client.set_parent, mutation.target, parent_key)
        except urllib.error.HTTPError as exc:
            # Hierarchy guard (ticket 8b25): on this next-gen project only an
            # Epic may be a parent; a Task→Task reparent (and any other unmet
            # hierarchy constraint) is rejected by Jira with HTTP 400 carrying
            # a misleading "same project" message. Treat any 400 as a
            # hierarchy rejection: WARN + continue the pass. Non-400 errors
            # keep the legacy generic-warning behaviour (still non-fatal).
            if exc.code == 400:
                logger.warning(
                    "parent sync skipped: Jira hierarchy rejected %s→%s",
                    mutation.target,
                    parent_key,
                )
            else:
                logger.warning(
                    "_apply_outbound_update: set_parent failed for %s parent=%r: %r",
                    mutation.target,
                    parent_key,
                    exc,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_apply_outbound_update: set_parent failed for %s parent=%r: %r",
                mutation.target,
                parent_key,
                exc,
            )
    if allowed:
        _call_with_retry(client.update_issue, mutation.target, **allowed)

    # Dispatch label mutations: add_label / remove_label per entry.
    # Mirrors update_one's label-dispatch logic (bug 87e4) for the typed-leaf path.
    # Gap fix (bugs 3b5f / 85a1): _apply_outbound_update previously ignored
    # payload['labels'] entirely, causing label changes to silently no-op when
    # this leaf was invoked directly (single typed-mutation dispatch path).
    labels = payload.get("labels") or []
    labels_applied: list[str] = []
    if isinstance(labels, list):
        for entry in labels:
            if not isinstance(entry, dict):
                continue
            action = entry.get("action")
            label_name = entry.get("label", "")
            if not label_name:
                continue
            try:
                if action == "add":
                    _call_with_retry(client.add_label, mutation.target, label_name)
                    labels_applied.append(f"+{label_name}")
                elif action == "remove":
                    _call_with_retry(client.remove_label, mutation.target, label_name)
                    labels_applied.append(f"-{label_name}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_apply_outbound_update: label %s failed for %s label=%r: %r",
                    action,
                    mutation.target,
                    label_name,
                    exc,
                )

    # Dispatch comment mutations: add_comment per entry.
    # Mirrors update_one's comment-dispatch logic (bug 87e4) for the typed-leaf path.
    # Gap fix (bugs 3b5f / 85a1): payload['comments'] was also silently dropped.
    # Note: outbound comment bodies are pre-decorated with RECONCILER_MARKER by
    # the outbound differ (_diff_comments → _decorate_outbound_comment) before
    # being placed in the mutation payload. The applier emits them as-is — no
    # decoration happens here to avoid double-decoration.
    comments = payload.get("comments") or []
    comments_applied: int = 0
    comment_errors: list[str] = []
    if isinstance(comments, list):
        for entry in comments:
            if not isinstance(entry, dict):
                continue
            body = entry.get("body", "")
            if not body:
                continue
            try:
                _call_with_retry(client.add_comment, mutation.target, body)
                comments_applied += 1
            except Exception as exc:  # noqa: BLE001
                # Bug 6afc-20ee-84e5-4dd5: non-fatal, but surface in the result
                # payload so a swallowed comment failure is observable in the
                # outcome instead of vanishing into the log.
                comment_errors.append(f"add_comment failed: {exc!s}")
                logger.warning(
                    "_apply_outbound_update: add_comment failed for %s: %r",
                    mutation.target,
                    exc,
                )

    # Loud skip guard: warn when the effective work set is entirely empty so
    # callers can distinguish a genuine no-op (no diff) from a misconfigured
    # mutation that carried only non-allowlisted fields.
    # parent_key is counted as work even when popped from allowed (ticket 8b25).
    if (
        not allowed
        and not labels_applied
        and not comments_applied
        and parent_key is None
    ):
        logger.warning(
            "_apply_outbound_update: no-op for %s — changed_fields %r "
            "produced zero allowlisted fields and no labels/comments; "
            "verify mutation payload is not empty or mis-keyed",
            mutation.target,
            list(changed_fields.keys()) if changed_fields else [],
        )

    result_payload: dict[str, Any] = {
        "fields_pushed": sorted(allowed.keys()),
        "labels_applied": labels_applied,
        "comments_applied": comments_applied,
    }
    if comment_errors:
        result_payload["comment_errors"] = comment_errors
    if parent_key is not None:
        result_payload["parent_set"] = parent_key

    return ApplyResult(mutation.direction, mutation.action, result_payload)


def _apply_outbound_delete(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Outbound delete: route through the legacy batch path's delete_one()
    when a client is supplied. Typed-mutation callers can also drive a direct
    delete via this leaf.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.outbound)
    if client is None:
        # Stub path: preserved for tests that don't exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})
    try:
        _call_with_retry(client.delete_issue, mutation.target)
    except JiraAPIError as exc:
        if getattr(exc, "status_code", None) == 404:
            # Already-gone is the post-state we want — treat as success.
            return ApplyResult(
                mutation.direction, mutation.action, {"already_gone": True}
            )
        raise
    return ApplyResult(
        mutation.direction, mutation.action, {"deleted": mutation.target}
    )


def _apply_outbound_probe(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Outbound probe: read-only sanity check via client.get_issue when supplied.

    Returns the probe outcome (key + present flag) in the result payload so
    upstream callers can branch on the live Jira state.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.outbound)
    if client is None or not hasattr(client, "get_issue"):
        return ApplyResult(mutation.direction, mutation.action, {})
    try:
        info = _call_with_retry(client.get_issue, mutation.target)
        return ApplyResult(
            mutation.direction,
            mutation.action,
            {"present": True, "issue": info if isinstance(info, dict) else {}},
        )
    except JiraAPIError as exc:
        if getattr(exc, "status_code", None) in (404, 410, 403):
            return ApplyResult(mutation.direction, mutation.action, {"present": False})
        raise


def _apply_outbound_conflict(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Outbound conflict: emit a structured conflict-marker comment on the Jira
    issue when a client is supplied. Conflicts are durable signals — the
    follow-on is consumed by reconcile_once via the standard suppress_pair
    channel so the same pair is not retried mid-pass.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.outbound)
    payload = dict(mutation.payload or {})
    if client is not None and hasattr(client, "add_comment"):
        try:
            _call_with_retry(
                client.add_comment,
                mutation.target,
                f"reconciler conflict detected: {payload.get('reason', 'unspecified')}",
            )
        except Exception:
            # Best-effort comment; do not propagate — the suppress_pair
            # follow-on still informs reconcile_once to drop further work.
            pass
    follow_on = {
        "kind": "suppress_pair",
        "local_id": payload.get("local_id", ""),
        "jira_key": mutation.target,
    }
    return ApplyResult(mutation.direction, mutation.action, {"follow_on": follow_on})


# ---------------------------------------------------------------------------
# Inbound leaf-body helpers (story bd19-d744-b8c7-4079)
#
# Inbound leaves write local ticket-tracker events directly because the local
# CLI is the authoritative reader and we want deterministic file-shape control.
# Event files follow the format documented at
# docs/ticket-system-v3-architecture.md and mirrored
# throughout the tracker dir as <ticket_id>/<ts>-<uuid>-<EVENT>.json.
# ---------------------------------------------------------------------------

# Jira→local translation + local-event-store IO live in inbound_translate.py.
# Re-imported here so the inbound leaves (still resident on this facade) and
# apply()'s suppression index resolve them as module globals.
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


def _apply_inbound_create(
    mutation, *, client=None, repo_root=None, binding_store=None
) -> ApplyResult:
    """Materialise a remote Jira issue as a local jira-* ticket.

    Writes a CREATE event (title, ticket_type, priority, description, tags
    including ``imported:reconciler-bootstrap``) and, when the payload carries
    a non-default status, a follow-up STATUS event reverse-mapped via
    config.local_to_jira_status.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    # Accept both shapes: payload with nested "fields" key (batch-dict shape)
    # and payload with top-level field keys (differ Mutation shape).
    fields = payload.get("fields") or payload
    jira_key = mutation.target
    local_id = _jira_key_to_local_id(jira_key)

    # Defense-in-depth inbound dedup (ticket 1577). The snapshot differ normally
    # stands down for an already-bound issue by recognising its rebar-id:<local_id>
    # label in the fetched fields (bug 4354) — that is the primary production
    # dedup. This guard covers the narrow transient where the snapshot predates
    # the label write-back yet the binding already exists in bindings.json: the
    # differ then mis-emits an inbound CREATE which would materialise a duplicate
    # local ticket. If the target Jira key is already bound, record the mapping
    # and skip materialisation. Cheap (local reverse-index lookup, no Jira GET).
    if binding_store is not None:
        bound_local_id = binding_store.get_local_id(jira_key)
        if bound_local_id:
            if repo_root is None:
                repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])
            mapping_path = repo_root / "bridge_state" / "mapping.json"
            _write_mapping_atomic(mapping_path, bound_local_id, jira_key)
            return ApplyResult(
                mutation.direction,
                mutation.action,
                {
                    "local_id": bound_local_id,
                    "jira_key": jira_key,
                    "dedup_skipped": True,
                },
            )

    issuetype = _extract_name(fields.get("issuetype"), "Task")
    ticket_type = _JIRA_TYPE_MAP.get(issuetype, "task")

    tracker_dir = _resolve_tracker_dir(repo_root)
    # Labels live at the top level of both payload shapes (the flat differ
    # shape IS fields); check fields first so a nested-"fields" payload that
    # carries labels inside the wrapper is not missed.
    raw_labels = list(fields.get("labels") or payload.get("labels") or [])
    # Bridge-internal labels (rebar-id:*, rebar-status:*) are reconciler
    # bookkeeping — they must not leak into local tags (the differs exclude
    # them from label sync, so a leaked tag would never converge away).
    tags = [
        t
        for t in raw_labels
        if not (isinstance(t, str) and t.startswith(_BRIDGE_INTERNAL_TAG_PREFIXES))
    ]
    if "imported:reconciler-bootstrap" not in tags:
        tags.append("imported:reconciler-bootstrap")
    # Parent sync (ticket 8b25): resolve the Jira parent field to a local id.
    # Three sources, in priority order:
    #   1. payload["_parent_local_id"] — pre-resolved by reconcile.py for the
    #      normal inbound-create path (reconcile already has binding_store).
    #   2. fields["parent"]["key"] derived via _jira_key_to_local_id —
    #      best-effort local-id derivation for jira-originated parents where the
    #      local id is deterministic (jira-dig-N convention).
    #   3. Empty string — safe fallback (hardcoded prior behaviour).
    _raw_parent = fields.get("parent")
    if payload.get("_parent_local_id"):
        resolved_parent_id = payload["_parent_local_id"]
    elif isinstance(_raw_parent, dict) and _raw_parent.get("key"):
        resolved_parent_id = _jira_key_to_local_id(_raw_parent["key"])
    else:
        resolved_parent_id = ""

    create_data: dict[str, Any] = {
        "id": local_id,
        "ticket_type": ticket_type,
        "title": fields.get("summary", "") or jira_key,
        # Live snapshots carry description as a raw ADF dict — normalize to
        # plain text or the CREATE event stores a dict where the reducer
        # expects a string (bug 1bb2 class), and the outbound differ then
        # re-pushes the description on EVERY pass (dict never equals the
        # ADF-decoded snapshot text). Ticket robe-creek-zealot.
        "description": _normalize_adf_body(fields.get("description")),
        "parent_id": resolved_parent_id,
        "tags": tags,
    }
    if "priority" in fields:
        create_data["priority"] = _resolve_priority(fields["priority"])
    if fields.get("assignee"):
        create_data["assignee"] = _extract_name(fields["assignee"])
    create_path = _write_event_file(tracker_dir, local_id, "CREATE", create_data)

    # Status: write a STATUS event when the Jira status reverse-maps to
    # something other than the reducer default ('open'). A rebar-status:
    # annotation label takes precedence over the raw workflow status —
    # same precedence as inbound_differ._map_jira_to_local_fields, so the
    # import lands at the status the bound-ticket differ would compute.
    local_status: str | None = None
    for _lbl in raw_labels:
        if isinstance(_lbl, str) and _lbl in _REBAR_STATUS_LABEL_TO_LOCAL:
            local_status = _REBAR_STATUS_LABEL_TO_LOCAL[_lbl]
            break
    if local_status is None:
        jira_status = _extract_name(fields.get("status"))
        if jira_status:
            local_status = _jira_status_to_local(jira_status)
    if local_status and local_status != "open":
        _write_event_file(
            tracker_dir,
            local_id,
            "STATUS",
            {"status": local_status, "current_status": "open"},
        )

    # Record the local<->jira binding NOW — we hold both ids. Without this,
    # the outbound differ (whose bound/unbound signal is the binding store
    # ALONE) sees the freshly imported ticket as unbound on the next pass and
    # re-emits an outbound CREATE for it, converging only via create_one's
    # dedup-skip JQL search — one wasted create + per-ticket Jira search per
    # imported ticket (ticket robe-creek-zealot, 1-pass idempotency).
    if binding_store is not None:
        _bind_confirm = getattr(binding_store, "bind_confirm", None)
        if _bind_confirm is not None:
            _bind_confirm(local_id, jira_key)
        else:
            import sys as _sys

            print(  # noqa: T201
                f"WARNING: inbound_create: binding store lacks bind_confirm; "
                f"{local_id!r}<->{jira_key!r} NOT bound at create — the next "
                f"pass will re-emit an outbound create (dedup-skip will "
                f"converge it).",
                file=_sys.stderr,
            )

    # Write rebar-id label + local_id entity property back to Jira so the
    # differ recognizes this issue as mirrored on subsequent passes (dedup).
    if client is not None:
        _call_with_retry(client.add_label, jira_key, f"rebar-id:{local_id}")
        _call_with_retry(client.set_entity_property, jira_key, "local_id", local_id)

    # Bug 221b: bootstrap pre-existing Jira comments so the local ticket has
    # a complete comment history immediately after inbound create.
    #
    # Strategy (mirrors _diff_comments_inbound in inbound_differ.py):
    #   1. Fetch the issue's comments via client.get_comments(jira_key).
    #   2. Skip any comment whose body (ADF-normalized) contains the
    #      loop-breaker marker — those are our own outbound echoes.
    #   3. Normalize ADF bodies to plain text (via _normalize_adf_body).
    #   4. Write one COMMENT event per remaining comment, recording
    #      jira_comment_id so the next-pass inbound comment diff dedupes.
    #
    # On get_comments failure: log a warning and skip comment bootstrap —
    # the CREATE still succeeds.
    if client is not None:
        try:
            raw_comments = client.get_comments(jira_key)
            if not isinstance(raw_comments, list):
                raw_comments = []
        except Exception as exc:  # noqa: BLE001
            import sys as _sys

            print(  # noqa: T201
                f"WARNING: inbound_create: get_comments for {jira_key!r} failed "
                f"({exc!r}). Skipping comment bootstrap — ticket created without "
                f"pre-existing comments. Alert: jira_key={jira_key!r}",
                file=_sys.stderr,
            )
            raw_comments = []

        for jc in raw_comments:
            if not isinstance(jc, dict):
                continue
            jid = jc.get("id")
            if jid is None:
                continue
            body_text = _normalize_adf_body(jc.get("body"))
            if _RECONCILER_MARKER_APPLIER in body_text:
                continue  # outbound echo — skip
            if not body_text.strip():
                continue
            event_data: dict[str, Any] = {
                "body": body_text,
                "jira_comment_id": str(jid),
            }
            _write_event_file(tracker_dir, local_id, "COMMENT", event_data)

    return ApplyResult(
        mutation.direction,
        mutation.action,
        {"local_id": local_id, "create_event": str(create_path)},
    )


def _apply_inbound_update(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Apply a remote-side update to an existing local jira-* ticket.

    Writes one EDIT event with the changed fields, plus an additional STATUS
    event when the payload includes a Jira status change. Unknown ticket
    directories are tolerated (the EDIT is still written; the reducer will
    surface fsck on the next read).
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    # Accept both shapes: payload with nested "fields" key (batch-dict shape)
    # and payload with top-level field keys (differ Mutation shape).
    fields = payload.get("fields") or payload
    target = mutation.target
    # Bug 1bb2: prefer payload['local_id'] (set by reconcile.py for bound
    # tickets) over Jira-key-derived local_id. Without this, EDIT events
    # for a bound UUID ticket are written under a new jira-dig-NNN/
    # directory — creating a duplicate ticket and silently dropping the
    # update on the bound UUID ticket.
    local_id = payload.get("local_id") or (
        target if target.startswith("jira-") else _jira_key_to_local_id(target)
    )
    tracker_dir = _resolve_tracker_dir(repo_root)

    # Map field names to local reducer field names. The inbound differ
    # ALREADY maps Jira → local (see inbound_differ._map_jira_to_local_fields:
    # emits ``title`` / ``ticket_type`` / local-mapped ``status``). Accept
    # the local-keyed shape from the differ AND the legacy Jira-keyed
    # ``summary`` for back-compat with any caller that bypasses the differ.
    edit_fields: dict[str, Any] = {}
    if "title" in fields:
        edit_fields["title"] = fields["title"]
    elif "summary" in fields:
        edit_fields["title"] = fields["summary"]
    if "description" in fields:
        desc = fields["description"]
        # Bug 1bb2: normalize ADF dict → plain text. The differ should
        # normalize at read time, but guard here too in case a caller
        # forwards the raw ADF dict (defense-in-depth).
        if isinstance(desc, dict):
            desc = _normalize_adf_body(desc)
        edit_fields["description"] = desc
    if "priority" in fields:
        edit_fields["priority"] = _resolve_priority(fields["priority"])
    if "assignee" in fields:
        edit_fields["assignee"] = _extract_name(fields["assignee"])
    if "ticket_type" in fields:
        edit_fields["ticket_type"] = fields["ticket_type"]
    # Parent sync (ticket 8b25): inbound parent_id change → include in EDIT.
    # The inbound differ surfaces this as ``fields["parent_id"] = <local_id>``.
    if "parent_id" in fields:
        edit_fields["parent_id"] = fields["parent_id"] or ""

    written: list[str] = []
    if edit_fields:
        path = _write_event_file(tracker_dir, local_id, "EDIT", {"fields": edit_fields})
        written.append(str(path))

    if "status" in fields:
        raw_status = fields["status"]
        # Bug 1bb2 (llm-review finding 2): Trust the differ contract by
        # SHAPE rather than VALUE. The inbound differ
        # (_diff_jira_vs_local → _map_jira_to_local_fields) always emits
        # status as a pre-mapped local string. Legacy callers that bypass
        # the differ pass the raw Jira shape (a dict like {"name": "..."}).
        # A value-membership check would mis-route a Jira tenant whose
        # status is literally named one of the local values (e.g.
        # 'in_progress') — but only the dict shape needs reverse-mapping,
        # so the type itself is a reliable discriminator.
        if isinstance(raw_status, dict):
            local_status = _jira_status_to_local(_extract_name(raw_status))
        else:
            local_status = raw_status
        # current_status is the PREVIOUS state (matched against state["status"]
        # by the reducer for fork detection — see
        # ticket_reducer/_processors.py:process_status).
        # Read the latest STATUS event from the ticket dir to obtain it.
        previous_status = _read_latest_status(tracker_dir, local_id)
        path = _write_event_file(
            tracker_dir,
            local_id,
            "STATUS",
            {"status": local_status, "current_status": previous_status},
        )
        written.append(str(path))

    # Bug 57b0: inbound labels — apply payload['labels'] add/remove ops as
    # an EDIT event on `fields.tags`. The inbound differ surfaces label
    # mutations under ``payload['labels']`` as
    # ``[{"action": "add"|"remove", "label": "<name>"}]`` (see
    # inbound_differ._diff_labels_inbound). Without this block, every
    # inbound label add/remove was silently dropped — Jira-side label
    # changes never propagated to local tags.
    #
    # The local reducer treats tags as REPLACE-on-EDIT (see
    # ticket_reducer/_processors.process_edit), so we must read the
    # current tag list, apply the diff, and write the full resulting list.
    inbound_labels = payload.get("labels") or []
    if isinstance(inbound_labels, list) and inbound_labels:
        # Read current tags by reducing the existing ticket directory.
        #
        # Bug bc8f-775e-9a34-44d1: the local reducer treats `fields.tags` on
        # an EDIT event as REPLACE-the-whole-list (see
        # ticket_reducer/_processors.process_edit). If we cannot reliably
        # read the current tag list — either because reduce_ticket raises
        # OR because it returns None (ticket dir doesn't exist yet, e.g.,
        # race with a concurrent CREATE) — falling back to `current_tags=[]`
        # and writing `EDIT(fields.tags=[<just the new label>])` would WIPE
        # every pre-existing local tag. The live probe captured exactly
        # this: ticket b2e9 with `labelprobe-...` was reduced to `[]` after
        # T1's bidirectional pass.
        #
        # Safe behaviour: when current state cannot be read, SKIP the labels
        # EDIT for this pass and emit a stderr warning. The next reconciler
        # pass will retry once the ticket dir / state is readable.
        reducer_failed = False
        current_state: dict | None = None
        try:
            reducer_mod = _load_ticket_reducer()
            current_state = reducer_mod.reduce_ticket(str(tracker_dir / local_id))
        except Exception as _reducer_exc:  # noqa: BLE001 — see bc8f docstring
            reducer_failed = True
            print(
                f"[applier] WARN bc8f-guard: reducer raised while reading "
                f"current tags for {local_id} (target={mutation.target}); "
                f"skipping labels EDIT to avoid wiping local tags. "
                f"exc={type(_reducer_exc).__name__}: {_reducer_exc}",
                file=sys.stderr,
            )
        if not reducer_failed and current_state is None:
            print(
                f"[applier] WARN bc8f-guard: reducer returned None for "
                f"{local_id} (ticket dir not yet materialized?); skipping "
                f"labels EDIT to avoid wiping local tags. The next "
                f"reconciler pass will retry.",
                file=sys.stderr,
            )

        if current_state is not None:
            current_tags: list[str] = list(current_state.get("tags", []) or [])
            new_tags = list(current_tags)
            changed = False
            for entry in inbound_labels:
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action")
                label_name = entry.get("label", "")
                if not label_name or not isinstance(label_name, str):
                    continue
                if action == "add" and label_name not in new_tags:
                    new_tags.append(label_name)
                    changed = True
                elif action == "remove" and label_name in new_tags:
                    new_tags = [t for t in new_tags if t != label_name]
                    changed = True
            if changed:
                # Bug a06c: mark this EDIT event with source=inbound so
                # the local_label_intent computation skips it when
                # building the "user-intent" tag set. Without the marker,
                # a Jira-side ADD that the inbound differ applies locally
                # would enter local's tag history and then look identical
                # to user intent on the next pass — so a subsequent
                # Jira-side REMOVE would be cancelled by a spurious
                # outbound ADD (T4 IB-REMOVE regression).
                path = _write_event_file(
                    tracker_dir,
                    local_id,
                    "EDIT",
                    {"fields": {"tags": new_tags}, "source": "inbound"},
                )
                written.append(str(path))

    # Bug 85a1 (Gap 1): inbound comments — write a COMMENT event for each
    # new Jira comment the differ surfaced. The body is stored as plain
    # text (post-ADF-decode); ``jira_comment_id`` is persisted so the
    # outbound differ's loop-breaker skips this comment on the next pass.
    inbound_comments = payload.get("comments") or []
    if isinstance(inbound_comments, list):
        for entry in inbound_comments:
            if not isinstance(entry, dict):
                continue
            if entry.get("action") != "add":
                continue
            body = entry.get("body") or ""
            if not body:
                continue
            event_data: dict[str, Any] = {"body": body}
            jid = entry.get("jira_comment_id")
            if jid is not None:
                event_data["jira_comment_id"] = str(jid)
            path = _write_event_file(tracker_dir, local_id, "COMMENT", event_data)
            written.append(str(path))

    return ApplyResult(
        mutation.direction,
        mutation.action,
        {"local_id": local_id, "events": written},
    )


def _apply_inbound_delete(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Handle one of four probe-outcome branches when a Jira issue has
    disappeared from the working set.

    Branches (selected via ``mutation.payload['probe_outcome']``):
      * ``hard_delete``  — preserve the local content + emit a follow-on
        ``(outbound, create_after_hard_delete)`` mutation so reconcile_once
        re-creates the Jira side on the next applier pass.
      * ``redirect``     — rename the local jira-dig-NNN ticket directory to
        the new key supplied under ``new_jira_key``.
      * ``out_of_window``— write a COMMENT event noting the Jira issue is
        closed and aged out of the working set (no local mutation otherwise).
      * ``trash``        — write a COMMENT event noting recoverable trash state.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    branch = payload.get("probe_outcome", "out_of_window")
    target = mutation.target
    local_id = target if target.startswith("jira-") else _jira_key_to_local_id(target)
    tracker_dir = _resolve_tracker_dir(repo_root)
    result_payload: dict[str, Any] = {"branch": branch, "local_id": local_id}

    if branch == "hard_delete":
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {
                "comment": (
                    f"reconciler: Jira issue {target} hard-deleted; local "
                    "content preserved. Outbound re-create follow-on emitted."
                )
            },
        )
        follow_on = {
            "direction": "outbound",
            "action": "create_after_hard_delete",
            "target": target,
            "local_id": local_id,
        }
        result_payload["follow_on"] = follow_on
        # TODO(epic-3e36): wire the follow-on mutation into reconcile_once so
        # the outbound re-create runs in the same pass. Tracked separately.
    elif branch == "redirect":
        new_key = payload.get("new_jira_key", "")
        new_local_id = (
            _jira_key_to_local_id(new_key) if new_key else local_id + "-redirected"
        )
        src = tracker_dir / local_id
        dst = tracker_dir / new_local_id
        # Collision protection (PR #375 review thread 3307104042): when both
        # src and dst already exist on disk (prior failed pass, or the
        # destination key was imported by another path) we cannot silently
        # skip the rename — that leaves the tracker holding two ticket dirs
        # for the same logical ticket. Raise so the operator can reconcile.
        if src.exists() and dst.exists():
            raise FileExistsError(
                f"inbound delete redirect: refusing to rename {src} -> {dst} "
                f"because destination already exists (target={target}, "
                f"new_jira_key={new_key!r})"
            )
        if src.exists() and not dst.exists():
            src.rename(dst)
        # Write a comment noting the redirect on the destination directory.
        _write_event_file(
            tracker_dir,
            new_local_id,
            "COMMENT",
            {"comment": f"reconciler: redirected from {target} -> {new_key}"},
        )
        result_payload["new_local_id"] = new_local_id
    elif branch == "out_of_window":
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {
                "comment": (
                    f"reconciler: Jira issue {target} is closed and has aged "
                    "out of the working window."
                )
            },
        )
    elif branch == "trash":
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {
                "comment": (
                    f"reconciler: Jira issue {target} entered recoverable trash state."
                )
            },
        )
    else:
        # Unknown branch: record observable evidence; do not raise so the pass
        # converges. The structural test enforces real-body coverage.
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {"comment": f"reconciler: unknown probe_outcome={branch!r}"},
        )

    return ApplyResult(mutation.direction, mutation.action, result_payload)


def _apply_inbound_probe(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Inbound probe leaf: probe execution lives in reconcile.route_inbound_probe.

    The leaf itself is a marker — the probe classification and follow-on
    generation happen upstream of applier dispatch. We still write an audit
    comment so the dispatch path is observable in the local tracker when a
    probe leaf is invoked directly.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)
    target = mutation.target
    local_id = target if target.startswith("jira-") else _jira_key_to_local_id(target)
    tracker_dir = _resolve_tracker_dir(repo_root)
    ticket_dir = tracker_dir / local_id
    if ticket_dir.exists():
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {"comment": f"reconciler: inbound probe acknowledged for {target}"},
        )
    return ApplyResult(
        mutation.direction, mutation.action, {"local_id": local_id, "probed": target}
    )


def _apply_inbound_clean_label(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Remove rebar-id-* labels from a Jira issue.

    Inbound-only leaf: invoked when the differ has detected stale or duplicated
    `rebar-id-*` labels on the Jira side that need to be removed. The mutation
    payload carries the labels to remove under ``labels_to_remove``; only labels
    that match the ``rebar-id-*`` pattern are removed (defensive filter against a
    misshapen payload). All client calls go through :func:`_call_with_retry`
    so transient 5xx/429/timeout failures retry with backoff.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)
    if client is None:
        # Stub path: preserved for tests that don't exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})
    labels = mutation.payload.get("labels_to_remove") or []
    removed: list[str] = []
    for label in labels:
        # Defensive: only remove labels matching the rebar-id-* pattern.
        if not isinstance(label, str) or not label.startswith("rebar-id-"):
            continue
        _call_with_retry(client.remove_label, mutation.target, label)
        removed.append(label)
    return ApplyResult(mutation.direction, mutation.action, {"removed": removed})


def _apply_inbound_repair_property(
    mutation, *, client=None, repo_root=None
) -> ApplyResult:
    """Repair a missing ``local_id`` entity property on a Jira issue.

    Delegates to the existing :func:`inbound_repair_property` implementation
    (kept under its legacy name for back-compat with existing tests). Wraps
    the outcome dict into an ``ApplyResult`` so it routes cleanly through the
    typed-mutation dispatch table.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)
    if client is None:
        # Stub path: preserved for tests that don't exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})
    outcome = inbound_repair_property(mutation, client)
    return ApplyResult(mutation.direction, mutation.action, outcome)


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


def _apply_inbound_conflict(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Emit a ``suppress_pair`` follow-on and a ``pending_bug_ticket`` directive
    for an unresolved (local, Jira) conflict.

    Bug filing is DEFERRED out of the apply loop because the
    ``rebar create bug`` CLI commits to the ``tickets``
    orphan branch, which would advance HEAD inside ``_apply_batch``'s
    drift-guarded loop and raise a spurious ``HeadDriftError`` (bug d822).
    The caller (``apply``) collects pending_bug_ticket directives during the
    inbound dispatch loop and files them after ``_apply_batch`` returns —
    outside the drift guard's scope.

    The follow_on still emits during the leaf so reconcile_once can drop
    subsequent mutations for the same pair in the same pass; suppression
    semantics are unchanged.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    jira_key = mutation.target
    local_id = payload.get("local_id", "")
    reason = payload.get("reason", "unspecified")
    parent_id = payload.get("parent_id") or _rebar_env(
        "RECONCILER_CONFLICT_PARENT_ID", ""
    )
    title = f"[Reconciler conflict]: pair ({local_id!r}, {jira_key!r}) -> {reason}"
    description = (
        f"Reconciler detected a conflict on (local_id={local_id!r}, "
        f"jira_key={jira_key!r}).\n\n"
        f"Reason: {reason}\n\n"
        "## Expected Behavior\n"
        "Conflict is resolved or suppressed before the next reconciler pass.\n\n"
        "## Actual Behavior\n"
        f"Conflict surfaced during applier dispatch with reason={reason!r}."
    )

    follow_on = {
        "kind": "suppress_pair",
        "local_id": local_id,
        "jira_key": jira_key,
    }
    pending_bug_ticket = {
        "title": title,
        "description": description,
        "parent_id": parent_id,
        "local_id": local_id,
        "jira_key": jira_key,
    }
    return ApplyResult(
        mutation.direction,
        mutation.action,
        {
            "follow_on": follow_on,
            "pending_bug_ticket": pending_bug_ticket,
        },
    )


def _build_leaves() -> dict[tuple[Any, Any], Callable[..., ApplyResult]]:
    """Build the _LEAVES registry.

    Built lazily-but-eagerly (at module import) by walking mutation._VALID_COMBINATIONS
    and binding the leaf handler for each pair. Only pairs in _VALID_COMBINATIONS
    are registered — invalid pairs (e.g. outbound + clean_label) are not present
    by construction.
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
        (D.inbound, A.delete): _apply_inbound_delete,
        (D.inbound, A.probe): _apply_inbound_probe,
        (D.inbound, A.clean_label): _apply_inbound_clean_label,
        (D.inbound, A.repair_property): _apply_inbound_repair_property,
        (D.inbound, A.conflict): _apply_inbound_conflict,
    }
    # Filter to only valid combinations — single source of truth is mutation.py.
    valid = mut_mod._VALID_COMBINATIONS
    return {k: v for k, v in handlers.items() if k in valid}


# The dispatch registry. Keys are (MutationDirection, MutationAction) tuples;
# values are leaf handler callables of shape (mutation, *, client=None) -> ApplyResult.
_LEAVES: dict[tuple[Any, Any], Callable[..., ApplyResult]] = _build_leaves()


# ---------------------------------------------------------------------------
# rebar-id label write authorization contract
# ---------------------------------------------------------------------------

# Justification for the F841 suppression below: this constant is read by
# tests/unit/rebar_reconciler/test_errors.py::test_authorized_writers_docstring
# _documents_full_contract via getattr — static analyzers cannot trace the
# usage. Do NOT remove; it is the contract artifact for story 4496 dd-1.
_AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC: str = """  # noqa: F841
rebar-id label write authorization contract for applier.py
=========================================================

The applier dispatches mutations through exactly 9 leaf handlers, listed below
with their authorization status for rebar-id label mutations:

  1. outbound_create       — AUTHORIZED for {create}: adds "rebar-id:<local_id>"
                             label when a new Jira issue is created outbound.
  2. outbound_update       — UNAUTHORIZED for rebar-id label mutations.
  3. outbound_delete       — UNAUTHORIZED for rebar-id label mutations.
  4. outbound_probe        — UNAUTHORIZED for rebar-id label mutations.
  5. outbound_conflict     — UNAUTHORIZED for rebar-id label mutations.
  6. inbound_create        — AUTHORIZED for {create}: adds "rebar-id:<local_id>"
                             label when a new local ticket is created inbound
                             (dedup write-back so the differ recognizes the
                             issue as mirrored on subsequent passes).
  7. inbound_update        — UNAUTHORIZED for rebar-id label mutations.
  8. inbound_clean_label   — AUTHORIZED for {delete}: removes stale or
                             duplicated "rebar-id-*" labels from the Jira side.
  9. inbound_repair_property — UNAUTHORIZED for rebar-id label mutations.
                              This leaf writes the local_id entity PROPERTY
                              FIELD via set_issue_property(), NOT the label.

Only inbound_clean_label (delete), outbound_create (create), and
inbound_create (create) may emit rebar-id label mutations. Any other leaf that emits such a mutation is a bug
and should raise RebarIdLabelWriteError from _errors.py.

conflict_resolver per-element provenance MUST skip rebar-id fields. The
conflict_resolver must not write, modify, or emit rebar-id label mutations;
rebar-id is the identity primitive and its provenance is governed solely by the
two authorized leaves above, not by the per-field provenance resolution path.

inbound_repair_property writes the local_id property field (entity
properties, not labels). It MUST NOT touch the label surface.
"""

_AUTHORIZED_REBAR_ID_LABEL_WRITERS: frozenset[str] = frozenset(
    {"inbound_clean_label", "outbound_create", "inbound_create"}
)
"""Leaf names authorized to emit rebar-id label mutations (see _AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC)."""

# Per-leaf authorized-action map: enforced by _audit_rebar_id_label_writes.
# Each authorized leaf is permitted ONLY the action(s) listed here; any other
# action on a rebar-id-* label by the same leaf raises RebarIdLabelWriteError. The
# pair set is the single source of truth referenced by
# _AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC above.
_AUTHORIZED_REBAR_ID_LABEL_ACTIONS: dict[str, frozenset[str]] = {
    "outbound_create": frozenset({"create"}),
    "inbound_create": frozenset({"create"}),
    "inbound_clean_label": frozenset({"delete"}),
}

# ---------------------------------------------------------------------------
# rebar-id label write guard
#
# _audit_rebar_id_label_writes is called after every leaf returns its mutation
# list (or before dispatching the typed-mutation leaf) to ensure no unauthorized
# leaf emits a rebar-id-* label mutation.
#
# Guard mode is controlled by REBAR_ID_GUARD_MODE (env) or rebar_id_guard_mode
# (.rebar/config.conf key). Precedence: env > config > default ('raise').
# ---------------------------------------------------------------------------


def _get_rebar_id_guard_mode_from_config() -> str | None:
    """Read rebar_id_guard_mode from the rebar config file, if present.

    Returns the value string (e.g. 'raise', 'warn') or None when the key
    is absent or the file cannot be read.

    Resolution order for the guard mode (env wins):
      1. os.environ['REBAR_ID_GUARD_MODE']  — checked in _audit_rebar_id_label_writes
      2. This function (.rebar/config.conf fallback)
      3. Default: 'raise'
    """
    try:
        _root = os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT")
        if os.environ.get("REBAR_CONFIG"):
            config_path = Path(os.environ["REBAR_CONFIG"])
        elif _root:
            config_path = Path(_root) / ".rebar" / "config.conf"
        else:
            config_path = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4]) / ".rebar" / "config.conf"
        if not config_path.exists():
            return None
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("rebar_id_guard_mode"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    return parts[1].strip().strip('"').strip("'")
    except OSError:
        # Best-effort config read: filesystem-level failures (permission denied,
        # missing parent dir on race, etc.) fall through to the default 'raise'
        # guard mode. Programming errors (AttributeError, TypeError) intentionally
        # propagate so they surface during test runs.
        return None
    return None


def _is_rebar_id_label_write_mutation(mutation) -> bool:
    """Return True when *mutation* represents a rebar-id-* label write.

    Checks two shapes:
    - String payload (direct audit call): mutation.target == 'label' AND
      mutation.payload.startswith('rebar-id-') AND action in {create,update,delete}.
    - Dict payload (full Mutation from apply()): payload contains 'target'=='label'
      AND 'label' value starts with 'rebar-id-' AND action in {create,update,delete}.
    """
    action = str(getattr(mutation, "action", ""))
    if action not in {"create", "update", "delete"}:
        return False
    payload = getattr(mutation, "payload", None)
    if isinstance(payload, str):
        # String payload: check target field and payload value
        target = getattr(mutation, "target", "")
        return target == "label" and payload.startswith("rebar-id-")
    elif isinstance(payload, dict):
        # Dict payload: check embedded 'target'=='label' and 'label' value
        embedded_target = payload.get("target", "")
        label_val = payload.get("label", "")
        if (
            embedded_target == "label"
            and isinstance(label_val, str)
            and label_val.startswith("rebar-id-")
        ):
            return True
    return False


def _audit_rebar_id_label_writes(leaf_name: str, mutations: list) -> None:
    """Guard: raise (or warn) when an unauthorized leaf emits a rebar-id-* label mutation.

    Called before leaf dispatch (`_apply_typed`) AND on each leaf invocation in
    the legacy batch path (`_apply_batch`) to enforce the two-authorized-leaves
    contract documented in `_AUTHORIZED_REBAR_ID_LABEL_WRITERS_DOC`.

    Per-action enforcement (wired via `_AUTHORIZED_REBAR_ID_LABEL_ACTIONS`):
      - When `leaf_name` is in `_AUTHORIZED_REBAR_ID_LABEL_WRITERS` but emits an
        action OUTSIDE its permitted action set (e.g., outbound_create
        attempting a `delete` on a rebar-id label), the guard still raises. The
        contract is per-action; defeating it would leave a security gap by
        allowing an authorized leaf to perform any action.

    Guard mode (REBAR_ID_GUARD_MODE env var, .rebar/config.conf key rebar_id_guard_mode,
    default 'raise'):
      - 'raise': RebarIdLabelWriteError raised on violation (default, production-safe).
      - 'warn': WARNING logged with tag REBAR_ID_GUARD; no exception raised (staged rollout).

    Precedence: env var > .rebar/config.conf key > default 'raise'.
    """
    is_authorized_leaf = leaf_name in _AUTHORIZED_REBAR_ID_LABEL_WRITERS
    allowed_actions = _AUTHORIZED_REBAR_ID_LABEL_ACTIONS.get(leaf_name, frozenset())

    offending = None
    offending_payload = None
    offending_action = None
    for mutation in mutations:
        if not _is_rebar_id_label_write_mutation(mutation):
            continue
        action_str = str(getattr(mutation, "action", ""))
        if is_authorized_leaf and action_str in allowed_actions:
            # Permitted (leaf, action) pair — skip without raising.
            continue
        offending = mutation
        offending_action = action_str
        # Extract the label payload for the error message
        payload = getattr(mutation, "payload", "")
        if isinstance(payload, str):
            offending_payload = payload
        elif isinstance(payload, dict):
            offending_payload = payload.get("label", str(payload))
        else:
            offending_payload = str(payload)
        break

    if offending is None:
        return

    # Determine guard mode: env > config > default 'raise'
    guard_mode = _rebar_env("ID_GUARD_MODE")
    if guard_mode is None:
        guard_mode = _get_rebar_id_guard_mode_from_config()
    if guard_mode is None:
        guard_mode = "raise"

    msg = (
        f"REBAR_ID_GUARD: unauthorized rebar-id label write from leaf '{leaf_name}' "
        f"(action={offending_action!r}); offending payload: {offending_payload!r}"
    )

    if guard_mode == "warn":
        logger.warning(msg)
        return

    errs = _load_errors_module()
    raise errs.RebarIdLabelWriteError(msg)


class _BatchAuditView:
    """Adapter exposing a legacy dict-shaped batch mutation to the audit guard.

    The audit (`_is_rebar_id_label_write_mutation`) expects an object with
    ``target``, ``payload`` (str OR dict), and ``action`` attributes. Legacy
    batch mutations are dicts of shape ``{"action": ..., "key": ..., "fields":
    {"labels": [...], ...}}`` — this view surfaces any rebar-id-* label values
    sitting under ``fields["labels"]`` as a synthetic label-write mutation so
    the guard fires on unauthorized batch paths (e.g., an outbound_update
    trying to push a rebar-id-* label).

    ``target`` is set to 'label' iff the batch mutation includes a rebar-id-*
    label in its fields; otherwise an empty string makes the audit pass-through.
    """

    __slots__ = ("target", "payload", "action")

    def __init__(self, batch_mutation: dict) -> None:
        self.action = batch_mutation.get("action", "")
        fields = batch_mutation.get("fields") or {}
        labels = fields.get("labels") if isinstance(fields, dict) else None
        rebar_id_label = None
        if isinstance(labels, (list, tuple)):
            for lbl in labels:
                if isinstance(lbl, str) and lbl.startswith("rebar-id-"):
                    rebar_id_label = lbl
                    break
        if rebar_id_label is not None:
            self.target = "label"
            self.payload = rebar_id_label
        else:
            # Synthesise an explicit non-label target so the guard's
            # _is_rebar_id_label_write_mutation returns False on benign batches.
            self.target = ""
            self.payload = ""


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
    ("inbound", "delete"): "inbound_delete",
    ("inbound", "probe"): "inbound_probe",
    ("inbound", "clean_label"): "inbound_clean_label",
    ("inbound", "repair_property"): "inbound_repair_property",
    ("inbound", "conflict"): "inbound_conflict",
}


def _apply_typed(mutation, *, client=None, repo_root=None, binding_store=None) -> ApplyResult:
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
            f"unknown (direction={mutation.direction.value!s}, "
            f"action={mutation.action.value!s})"
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
    # PR #375 review thread 3306949603).
    import inspect as _inspect

    try:
        sig = _inspect.signature(handler)
        _has_var_kw = any(
            p.kind is _inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
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
class JiraAPIError(Exception):
    """Exception raised by AcliClient stubs to simulate Jira HTTP error responses."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryExhaustedError(Exception):
    """Raised when _call_with_retry exhausts all retry attempts."""


def _call_with_retry(fn, *args, timeout_s: int = 30, max_retries: int = 3, **kwargs):
    """Call fn(*args, **kwargs) with exponential backoff on retryable failures.

    Retryable: TimeoutError, JiraAPIError with status 5xx, JiraAPIError with status 429.
    Non-retryable: JiraAPIError with 4xx (except 429) — re-raised immediately.
    On exhaustion of max_retries, raises RetryExhaustedError.

    Args:
        fn:          Callable to invoke.
        *args:       Positional arguments forwarded to fn.
        timeout_s:   Per-call timeout in seconds (currently advisory for stub-based callers).
        max_retries: Maximum number of retry attempts after the first failure.
        **kwargs:    Keyword arguments forwarded to fn.

    Returns:
        The return value of fn on success.

    Raises:
        RetryExhaustedError: When all retry attempts are exhausted.
        JiraAPIError:        Immediately, for non-retryable 4xx (except 429) errors.
    """
    delays = [1, 2, 4]
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except JiraAPIError as exc:
            # 429 and 5xx are retryable; all other 4xx fail fast
            if exc.status_code != 429 and 400 <= exc.status_code < 500:
                raise
            last_exc = exc
        except TimeoutError as exc:
            last_exc = exc

        if attempt < max_retries:
            delay = delays[min(attempt, len(delays) - 1)]
            time.sleep(delay)

    raise RetryExhaustedError(str(last_exc))


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


def create_one(
    mutation: dict,
    client,
    rest_calls: int = 0,
    deferred_creates: list | None = None,
    events_list: list | None = None,
    repo_root: Path | None = None,
    binding_store=None,
    comment_errors: list[str] | None = None,
) -> dict | None:
    """Create a Jira issue from the mutation's fields, with budget guard and JQL dedup.

    Budget guard: if rest_calls >= 200, appends mutation to deferred_creates and
    returns None without issuing any REST calls.

    JQL dedup: searches for an existing issue with label 'rebar-id:<local_id>' before
    creating. On hit, skips create_issue(), writes mapping.json atomically, appends a
    dedup-create-skipped event to events_list, and returns a dedup sentinel.
    On miss, proceeds with create_issue().

    Args:
        mutation:         Mutation dict with at least "fields" and optionally "local_id".
        client:           AcliClient instance.
        rest_calls:       Number of REST calls already issued in this pass.
        deferred_creates: List to append deferred mutations to (budget guard).
        events_list:      List to append structured events to (dedup hit events).
        repo_root:        Repository root for resolving bridge_state/mapping.json.
                          Defaults to four levels above this file when None.
        comment_errors:   Optional list collecting add_comment failures during the
                          post-create comment-dispatch loop (bug ea6d). When
                          provided, each failure is appended (string form) so the
                          caller can surface it in the batch outcome instead of
                          reporting error=None. Failures stay NON-fatal — the issue
                          create already succeeded. Passing ``None`` (the default)
                          preserves the legacy log-only behaviour, mirroring
                          update_one's comment_errors contract.

    Returns:
        The client.create_issue() result on miss, a dedup sentinel dict on hit,
        or None when the mutation is budget-deferred.
    """
    # Budget guard: defer without any REST call when at or over the limit
    if rest_calls >= 200:
        if deferred_creates is not None:
            deferred_creates.append(mutation)
        return None

    local_id = mutation.get("local_id", "")
    jql = f'labels = "rebar-id:{local_id}"'
    hits = client.search_issues(jql)

    if hits:
        hit_key = hits[0].get("key", "")

        # Persist local_id -> jira_key in mapping.json atomically
        if repo_root is None:
            repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])
        mapping_path = repo_root / "bridge_state" / "mapping.json"
        _write_mapping_atomic(mapping_path, local_id, hit_key)

        # Emit structured event into the caller's events list
        if events_list is not None:
            events_list.append(
                {
                    "event": "dedup-create-skipped",
                    "local_id": local_id,
                    "jira_key": hit_key,
                }
            )

        if binding_store is not None and local_id and hit_key:
            binding_store.bind_confirm(local_id, hit_key)
        return {"status": "dedup-create-skipped", "key": hit_key}

    # Translate differ-emitted Jira snapshot field names (summary, status,
    # issuetype) into the bridge schema (title, ticket_type) that
    # AcliClient.create_issue requires. Without this translation, create_issue
    # raises ValueError("title/summary is empty") because the differ never
    # emits a 'title' key. The mapping is conservative — only the two fields
    # AcliClient inspects are remapped; everything else passes through.
    _raw_fields = mutation.get("fields", {})
    _ticket_data = dict(_raw_fields)
    if "title" not in _ticket_data:
        # 'summary' is Jira's canonical field for the human-readable headline;
        # AcliClient.create_issue uses 'title' as the bridge-side equivalent.
        _ticket_data["title"] = _ticket_data.get("summary", "")
    if "ticket_type" not in _ticket_data:
        _issuetype = _ticket_data.get("issuetype")
        if isinstance(_issuetype, dict):
            _ticket_data["ticket_type"] = _issuetype.get("name", "Task")
        elif isinstance(_issuetype, str):
            _ticket_data["ticket_type"] = _issuetype
        else:
            _ticket_data["ticket_type"] = "Task"
    result = _call_with_retry(client.create_issue, _ticket_data)

    # Write identity markers so the issue can be re-discovered by dedup JQL
    # and by inbound consumers that inspect entity properties.
    jira_key = result.get("key", "") if isinstance(result, dict) else ""
    if jira_key:
        try:
            # Wrap identity writes in _call_with_retry so transient 5xx/429
            # absorb the same retry budget as create_issue above. Without this,
            # a single transient failure here triggers the unnecessary rollback
            # branch (delete_issue + BRIDGE_ALERT) even though the underlying
            # condition would have cleared on retry.
            _call_with_retry(client.add_label, jira_key, f"rebar-id:{local_id}")
            _call_with_retry(
                client.set_entity_property, jira_key, "local_id", local_id
            )
            if binding_store is not None and local_id:
                binding_store.bind_confirm(local_id, jira_key)
        except Exception as write_err:
            try:
                client.delete_issue(jira_key)
            except Exception:
                pass  # rollback failure must not mask original error
            # Emit BRIDGE_ALERT for identity-write rollback so the event is
            # surfaced in the tickets-tracker for observability.  # tickets-boundary-ok
            try:
                import uuid as _uuid
                import time as _time
                import json as _json

                _alert_root = (
                    repo_root or Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])
                ) / ".tickets-tracker"
                # F7: defensive guard — if local_id is falsy the alert directory
                # would resolve to .tickets-tracker root and pollute it. Prefer
                # the jira_key, falling back to a uuid so the alert always lands
                # under a non-root subdirectory.
                _alert_dir_key = local_id or jira_key or f"unknown-{_uuid.uuid4()}"
                _ticket_dir = _alert_root / _alert_dir_key
                _ticket_dir.mkdir(parents=True, exist_ok=True)
                _ts = _time.time_ns()
                _alert_uuid = str(_uuid.uuid4())
                _alert_path = _ticket_dir / f"{_ts}-{_alert_uuid}-BRIDGE_ALERT.json"
                _alert_path.write_text(
                    _json.dumps(
                        {
                            "event_type": "BRIDGE_ALERT",
                            "timestamp": _ts,
                            "uuid": _alert_uuid,
                            "ticket_id": local_id,
                            "jira_key": jira_key,
                            "data": {
                                "reason": "identity-write failed after create; Jira issue deleted",
                                "tag": "create-identity-write-failed",
                            },
                        }
                    )
                )
            except Exception:
                pass  # alert write failure must not mask original error
            raise write_err

    # Bug 85a1 (PR #87e4 follow-up): propagate user-supplied labels/comments
    # from the mutation payload after the identity-write block. The fix was
    # previously applied only to update_one (lines 1744-1779) — the symmetric
    # gap in create_one caused outbound CREATE to silently drop every user
    # label and comment, leaving freshly-created Jira issues with only the
    # rebar-id system label (Phase 1 of the e2e field-validation probe).
    # Failures here are logged but non-fatal — the create + identity write
    # already succeeded; a downstream label/comment dispatch failure must
    # not roll back the Jira issue.
    if jira_key:
        labels = mutation.get("labels", []) or []
        if isinstance(labels, list):
            for entry in labels:
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action")
                label_name = entry.get("label", "")
                if not label_name:
                    continue
                # remove-action entries are no-ops at CREATE time — a brand-new
                # issue has no preexisting labels to remove.
                if action != "add":
                    continue
                try:
                    _call_with_retry(client.add_label, jira_key, label_name)
                except Exception as exc:  # noqa: BLE001
                    print(  # noqa: T201
                        f"create_one: add_label failed for {jira_key} "
                        f"label={label_name!r}: {exc!r}",
                        file=sys.stderr,
                    )

        comments = mutation.get("comments", []) or []
        if isinstance(comments, list):
            for entry in comments:
                if not isinstance(entry, dict):
                    continue
                body = entry.get("body", "")
                if not body:
                    continue
                try:
                    _call_with_retry(client.add_comment, jira_key, body)
                except Exception as exc:  # noqa: BLE001
                    # Bug ea6d-e4b2-a316-45ec: non-fatal, but surface it so the
                    # batch outcome no longer reports error=None for an outbound
                    # CREATE whose comment sub-mutation failed. Mirrors update_one.
                    if comment_errors is not None:
                        comment_errors.append(f"add_comment failed: {exc!s}")
                    print(  # noqa: T201
                        f"create_one: add_comment failed for {jira_key}: {exc!r}",
                        file=sys.stderr,
                    )

    return result


def _is_illegal_transition_400(exc: Exception) -> bool:
    """Detect a 400 illegal-transition response from update_issue.

    Jira rejects status transitions that are not allowed from the current
    workflow state with a 400 response whose body mentions 'illegal' or
    'transition'. These are state errors (not transient), so they must not
    be retried.
    """
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code != 400:
        return False
    msg = str(exc).lower()
    return "illegal" in msg or "transition" in msg


def update_one(
    mutation: dict, client, comment_errors: list[str] | None = None
) -> dict | None:
    """Update an existing Jira issue from the mutation's key and fields.

    Bug 6afc-20ee-84e5-4dd5: comment sub-mutations (the ``comments`` payload)
    are applied as separate add_comment calls because Jira's edit endpoint
    cannot carry them. A failed add_comment is NON-fatal (the scalar update
    already succeeded) but must not be silently swallowed: when ``comment_errors``
    is provided, each add_comment failure is appended to it (string form) so the
    caller can surface it in the batch outcome instead of reporting error=None.
    Passing ``None`` (the default) preserves the legacy log-only behaviour for
    callers that do not collect comment errors.

    F3: AcliClient.update_issue's real signature is ``update_issue(jira_key, **kwargs)``;
    the field dict must be unpacked into keyword arguments rather than passed
    positionally as a single dict — otherwise Jira receives a TypeError on every
    real update call.

    Comment-fallback on 400 illegal-transition: when Jira rejects a status
    transition because it is not legal from the current workflow state, we do
    NOT retry (zero update_issue retries on 400 — it is a state error, not a
    transient). Instead we post a comment recording the local status change
    so an operator can see the divergence in Jira, and emit a structured log
    record to stderr.
    """
    fields = mutation.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    # Capture pre-filter status so the comment-fallback path (which reads
    # ``fields.get("status")`` after the allowlist strips it) can still
    # report the attempted local status (bug 85a1 follow-up).
    _attempted_status = fields.get("status")
    # Bug 85a1: strip fields ACLI does not accept on `jira workitem edit`.
    # The legacy batch path here was unfiltered, so a local issuetype change
    # (e.g., probe Phase 2 ticket_type=task→bug) flowed through as
    # ``--issuetype Bug`` which ACLI rejects with non-zero exit, aborting the
    # ENTIRE batch loop and silently losing every subsequent outbound update.
    # The typed leaf ``_apply_outbound_update`` already filters via
    # ``_OUTBOUND_UPDATE_ALLOWLIST`` — apply the same allowlist here. Stripped
    # fields (issuetype, type-change in general) are intentional drops mirroring
    # the typed-leaf contract; outbound issuetype changes are BY_DESIGN
    # unsupported on the edit endpoint (Atlassian JRASERVER-71292).
    # status is included: bug 85a1 (Gap 8) removed the BY_DESIGN drop —
    # outbound status push now uses REST POST /transitions via
    # ``transition_issue`` (bypasses ACLI's silent-exit-0 failure mode).
    # The typed leaf's REBAR_RECONCILER_STATUS_GATING gate is also gone.
    _OUTBOUND_BATCH_ALLOWLIST = frozenset(
        {"summary", "description", "assignee", "priority", "status"}
    )
    issue_key = mutation.get("key")
    # Parent reparent (ticket 8b25): the production outbound dispatch routes
    # through this legacy batch path, NOT the typed leaf _apply_outbound_update.
    # ACLI's ``jira workitem edit`` cannot reparent — the parent must go via
    # client.set_parent (REST PUT /rest/api/3/issue/{key} {"fields":{"parent"}}).
    # Before this fix, ``parent`` was not in _OUTBOUND_BATCH_ALLOWLIST, so it
    # was silently dropped as an "unaccepted field" and set_parent was never
    # called. The parent never landed, the next snapshot still showed no
    # parent, and the differ re-emitted the identical parent mutation on every
    # pass — the perpetual ``fields=['parent']`` re-emission (~230 steady-state
    # mutations, Phase-6 idempotency churn) AND the parent OUTBOUND CREATE/UPDATE
    # FAIL in the e2e field-validation probe. Mirror the typed leaf: pop parent
    # BEFORE the allowlist filter and route it through set_parent, guarding
    # HTTP 400 hierarchy rejections as non-fatal warnings.
    parent_key = fields.pop("parent", None)
    if parent_key is not None:
        try:
            _call_with_retry(client.set_parent, issue_key, parent_key)
        except urllib.error.HTTPError as exc:
            # Hierarchy guard (ticket 8b25): on this next-gen project only an
            # Epic may be a parent; a Task→Task reparent (and any other unmet
            # hierarchy constraint) is rejected with HTTP 400 carrying a
            # misleading "same project" message. Treat any 400 as a hierarchy
            # rejection: WARN + continue. Non-400 errors stay non-fatal too —
            # a parent failure must not abort the rest of the batch.
            if exc.code == 400:
                logger.warning(
                    "parent sync skipped: Jira hierarchy rejected %s->%s (HTTP 400)",
                    issue_key,
                    parent_key,
                )
            else:
                logger.warning(
                    "update_one: set_parent failed for %s parent=%r: %r",
                    issue_key,
                    parent_key,
                    exc,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "update_one: set_parent failed for %s parent=%r: %r",
                issue_key,
                parent_key,
                exc,
            )
    _stripped = {k: v for k, v in fields.items() if k not in _OUTBOUND_BATCH_ALLOWLIST}
    if _stripped:
        print(  # noqa: T201
            f"update_one: dropping fields not accepted by ACLI edit "
            f"for {mutation.get('key')}: {sorted(_stripped.keys())}",
            file=sys.stderr,
        )
    fields = {k: v for k, v in fields.items() if k in _OUTBOUND_BATCH_ALLOWLIST}
    # When the only changed field was parent (the common reparent case), the
    # allowlisted set is now empty AND set_parent already did the work — skip
    # the otherwise-empty client.update_issue call so we don't issue a no-op
    # ACLI edit purely to satisfy a parent-only mutation. The legacy
    # "empty fields still calls update_issue" contract is preserved for the
    # NON-parent case (e.g. an issuetype-only mutation that gets stripped):
    # update_issue is skipped here ONLY when a parent op was the reason the
    # field set is empty (label/comment dispatch below still runs).
    result: dict | None = None
    _skip_empty_update = parent_key is not None and not fields
    if _skip_empty_update:
        pass  # parent handled via set_parent; no scalar fields to edit
    else:
        try:
            result = _call_with_retry(client.update_issue, issue_key, **fields)
        except JiraAPIError as exc:
            if not _is_illegal_transition_400(exc):
                raise
            new_status = _attempted_status
            comment = f"local status changed to {new_status}"
            try:
                client.add_comment(issue_key, comment)
            except Exception:
                pass  # secondary failure must not mask the comment-fallback path
            log_entry = json.dumps(
                {
                    "action": "comment_fallback",
                    "issue_key": issue_key,
                    "attempted_status": _attempted_status,
                    "reason": "400_illegal_transition",
                }
            )
            print(log_entry, file=sys.stderr)
            result = None

    # Bug 87e4: propagate label add/remove and comment additions from the
    # mutation payload. The outbound differ emits these alongside changed
    # scalar fields; update_issue can't carry them (Jira's edit endpoint
    # doesn't accept label or comment kwargs), so they need separate
    # add_label / remove_label / add_comment calls. Failures here are
    # logged but non-fatal — the scalar update already succeeded.
    labels = mutation.get("labels", []) or []
    if isinstance(labels, list):
        for entry in labels:
            if not isinstance(entry, dict):
                continue
            action = entry.get("action")
            label_name = entry.get("label", "")
            if not label_name:
                continue
            try:
                if action == "add":
                    _call_with_retry(client.add_label, issue_key, label_name)
                elif action == "remove":
                    _call_with_retry(client.remove_label, issue_key, label_name)
            except Exception as exc:  # noqa: BLE001
                print(  # noqa: T201
                    f"update_one: label {action} failed for {issue_key} "
                    f"label={label_name!r}: {exc!r}",
                    file=sys.stderr,
                )

    comments = mutation.get("comments", []) or []
    if isinstance(comments, list):
        for entry in comments:
            if not isinstance(entry, dict):
                continue
            body = entry.get("body", "")
            if not body:
                continue
            try:
                _call_with_retry(client.add_comment, issue_key, body)
            except Exception as exc:  # noqa: BLE001
                # Bug 6afc-20ee-84e5-4dd5: non-fatal, but surface it so the batch
                # outcome no longer reports error=None for a mutation whose
                # comment sub-mutation failed.
                if comment_errors is not None:
                    comment_errors.append(f"add_comment failed: {exc!s}")
                print(  # noqa: T201
                    f"update_one: add_comment failed for {issue_key}: {exc!r}",
                    file=sys.stderr,
                )

    return result


def delete_one(mutation: dict, client) -> None:
    """Close a Jira issue by transitioning it to 'Closed'.

    F5: tolerate 404 — when the differ emits a delete it's precisely because
    the issue is no longer present in Jira; the subsequent transition_issue
    call therefore targets a key that may have already been removed. A 404 on
    the transition means the desired post-state ('issue gone') is already
    satisfied, so we treat it as success rather than letting the JiraAPIError
    unwind the entire pass. Other JiraAPIError statuses propagate normally.
    """
    # AcliClient exposes delete_issue (REST DELETE), not transition_issue.
    # The "close = transition to Closed" model belongs to a different bridge
    # surface that we don't use here — delete the Jira issue directly to
    # achieve the desired post-state ("issue gone from Jira").
    try:
        _call_with_retry(client.delete_issue, mutation.get("key"))
    except JiraAPIError as exc:
        if getattr(exc, "status_code", None) == 404:
            return  # already-gone is the goal of a delete mutation
        raise


def inbound_repair_property(mutation, client) -> dict:
    """Repair a missing entity property on a Jira issue.

    Happy path: invokes ``client.set_issue_property(target, 'local_id', local_id)``
    and returns ``{'status': 'ok', 'key': target}``.

    Failure path: when ``set_issue_property`` raises, attempts a follow-on cleanup
    via ``client.remove_label(target, 'rebar-id-<local_id>')`` (best-effort — a
    ``remove_label`` exception is captured, NOT raised), and returns an outcome
    dict with ``status='repair_property_failed'`` plus a top-level ``follow_on``
    payload whose ``kind`` is ``'schema_drift_signal'``. The follow-on is the
    signalling seam consumed by reconcile.py; this function MUST NOT import
    invariants directly — preserving the invariants-as-upstream-phase contract
    (see ticket 44e6-4916 AC: "applier.py does NOT import invariants").

    The follow_on field sits at the TOP LEVEL of the outcome dict (not nested
    under 'result'); manifest canonical-form serialization is expected to
    EXCLUDE follow_on fields when computing the content-addressable hash
    (per AC amendment G2 on ticket 44e6-4916).

    Args:
        mutation: Object exposing ``.target`` (Jira issue key) and ``.payload``
                  (mapping with at least a ``'local_id'`` entry).
        client:   AcliClient (or compatible test double) exposing
                  ``set_issue_property`` and ``remove_label``.

    Returns:
        Outcome dict — see status taxonomy above.
    """
    target = mutation.target
    payload = mutation.payload or {}
    local_id = payload.get("local_id", "")

    try:
        client.set_issue_property(target, "local_id", local_id)
        return {"status": "ok", "key": target, "follow_on": None}
    except Exception as exc:
        label_remove_err: Exception | None = None
        try:
            client.remove_label(target, f"rebar-id-{local_id}")
        except Exception as e:
            label_remove_err = e

        return {
            "status": "repair_property_failed",
            "key": target,
            "follow_on": {
                "kind": "schema_drift_signal",
                "issue_key": target,
                "reason": f"repair_property_failed: {exc}",
                "label_remove_error": (
                    str(label_remove_err) if label_remove_err is not None else None
                ),
            },
        }


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


def _mutation_to_batch_dict(mutation) -> dict:
    """Convert a Mutation dataclass instance to the legacy batch-dict shape.

    The legacy batch consumer (_apply_batch) expects a dict with keys:
    action, fields, key, local_id, follow_on, direction. Map the Mutation
    attributes accordingly so the batch path can iterate without crashing.

    Note: this dict is later passed through `json.dumps` when the manifest
    is written. Every value here MUST be JSON-serializable. Do NOT store
    the original Mutation object as a back-reference — non-serializable.
    """
    payload = dict(mutation.payload) if mutation.payload else {}
    action_value = getattr(mutation.action, "value", str(mutation.action))
    direction_value = getattr(mutation.direction, "value", str(mutation.direction))
    # Bug 87e4: outbound mutations from reconcile.py have two different
    # payload shapes depending on action:
    #
    #   - CREATE: payload has create fields at the TOP LEVEL (summary,
    #     description, priority, issuetype, assignee, ...) alongside
    #     bookkeeping keys (local_id, comments, labels). create_one needs
    #     the full set of fields.
    #
    #   - UPDATE: payload has changed fields under "changed_fields", with
    #     "comments" and "labels" as separate top-level keys. update_one
    #     needs ONLY the scalar field changes — passing the whole payload
    #     would unpack bogus `changed_fields=`, `comments=`, `labels=`
    #     kwargs to client.update_issue (the original bug symptom).
    #
    # Distinguish by action and read the appropriate shape.
    _BOOKKEEPING_KEYS = {
        "changed_fields",
        "comments",
        "labels",
        "local_id",
        "follow_on",
    }
    if action_value == "update":
        fields = payload.get("changed_fields")
        if fields is None:
            fields = payload.get("fields", {})
    elif action_value == "create":
        # Two CREATE payload shapes coexist:
        #   - Legacy (test fixtures + older callers): payload has a nested
        #     "fields" key.  Honor it explicitly (including the
        #     intentionally-empty {} case — the original "fields=={}
        #     must NOT fall through to full payload" contract).
        #   - New (reconcile.py:524-535): payload spreads create fields
        #     at the TOP LEVEL via `**om.fields`, alongside bookkeeping
        #     keys.  Strip bookkeeping; everything else is a field.
        if "fields" in payload:
            fields = payload.get("fields", {})
        else:
            fields = {k: v for k, v in payload.items() if k not in _BOOKKEEPING_KEYS}
    else:
        # Other actions (delete, probe, etc.) don't carry field maps.
        fields = payload.get("fields", {})
    return {
        "action": action_value,
        "direction": direction_value,
        "key": mutation.target,
        "fields": fields,
        "local_id": payload.get("local_id", ""),
        "follow_on": payload.get("follow_on"),
        # Surface comments and labels so update_one can dispatch them via
        # add_comment / add_label / remove_label respectively (bug 87e4).
        "comments": payload.get("comments", []),
        "labels": payload.get("labels", []),
    }


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
