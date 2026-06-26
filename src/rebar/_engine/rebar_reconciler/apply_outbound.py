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

import logging
from typing import Any

from rebar_reconciler._errors import is_not_found
from rebar_reconciler.apply_base import (
    ApplyResult,
    _direction_guard,
    _load_mutation_module,
)
from rebar_reconciler.batch_dispatch import (
    JiraAPIError,
    _call_with_retry,
    _mutation_to_batch_dict,
    update_one,
)

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
    except Exception:  # noqa: BLE001 — rollback path: best-effort delete of the issue created before the failure, then the ORIGINAL create error re-raises
        # Rollback path: if a Jira issue was (likely) created before the failure
        # surfaced, delete it via the same retry helper so transient delete
        # failures are also retried. Swallow any rollback error so the ORIGINAL
        # create exception is what re-raises to the caller.
        key = payload.get("key_hint") or mutation.target
        try:
            _call_with_retry(client.delete_issue, key)
        except Exception:  # noqa: BLE001 — rollback-must-not-mask-original
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
def _apply_outbound_update(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Outbound update — delegate to the ONE production applier (batch update_one).

    Story D (33d0) of epic f89d. Production applies every outbound update via
    ``batch_dispatch.update_one`` (the legacy batch path). This typed leaf used to
    carry a SECOND, parallel implementation of the same sub-ops (fields, labels,
    comments, links, parent) that production NEVER executed — and the two drifted:
    bug 3f04 dropped outbound links because the capability was re-added to the batch
    path while the differ/tests exercised only this leaf. That parallel logic is
    gone. The leaf now converts the Mutation to the batch dict and delegates to
    ``update_one``, so outbound-update application has a SINGLE source of truth. It
    stays a DISTINCT dispatch leaf (the typed registry requires one per valid
    direction x action) and mirrors ``_apply_outbound_delete``'s delegation pattern.

    Sub-op telemetry (``links_applied`` / ``comments_applied`` / ``labels_applied``)
    and the silent-no-op canary are intentionally NOT re-implemented here — surfacing
    them on the batch outcome is story E (2359). This leaf returns the raw
    ``update_one`` result plus any collected comment errors.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.outbound)

    if client is None:
        # Stub path: preserved for typed-dispatch coverage / tests that don't
        # exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})

    # batch_dispatch is a one-way dependency (it never imports the applier), so a
    # plain module-level import is safe — no cycle.
    batch = _mutation_to_batch_dict(mutation)
    comment_errors: list[str] = []
    update_result = update_one(batch, client, comment_errors=comment_errors)

    payload: dict[str, Any] = {"update_result": update_result}
    if comment_errors:
        payload["comment_errors"] = comment_errors
    return ApplyResult(mutation.direction, mutation.action, payload)


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
        if is_not_found(exc):
            # Already-gone is the post-state we want — treat as success.
            return ApplyResult(mutation.direction, mutation.action, {"already_gone": True})
        raise
    return ApplyResult(mutation.direction, mutation.action, {"deleted": mutation.target})


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
        except Exception:  # noqa: BLE001 — best-effort conflict comment; the suppress_pair follow-on still informs reconcile_once, so a failed comment is non-fatal
            # Best-effort comment; do not propagate — the suppress_pair
            # follow-on still informs reconcile_once to drop further work.
            pass
    follow_on = {
        "kind": "suppress_pair",
        "local_id": payload.get("local_id", ""),
        "jira_key": mutation.target,
    }
    return ApplyResult(mutation.direction, mutation.action, {"follow_on": follow_on})
