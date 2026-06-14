#!/usr/bin/env python3
"""Outbound leaf appliers: translate local mutations into Jira writes.

The five (outbound, *) leaf handlers dispatched from the typed registry, plus the
HEAD-drift subject helpers the batch loop uses to distinguish benign external
writers from competing reconciler writes. Each leaf calls _direction_guard, runs
its Jira side-effects through batch_dispatch._call_with_retry, and returns an
ApplyResult.

Imports downward only (apply_base, batch_dispatch); never imports applier.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
from pathlib import Path
from typing import Any

from rebar_reconciler.apply_base import (
    ApplyResult,
    _direction_guard,
    _load_mutation_module,
)
from rebar_reconciler.batch_dispatch import JiraAPIError, _call_with_retry, delete_one

logger = logging.getLogger(__name__)


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
