#!/usr/bin/env python3
"""Non-retrying ``update_one`` apply phases (module-size split of ``dispatch_one.py``).

``update_one`` is a thin sequencer over per-phase helpers. This leaf owns the phases
that do NOT funnel their Jira writes through ``dispatch_one._call_with_retry``: the
allowlist filter, the reporter-by-accountId REST sub-call (264f) and its identity /
alert-store degradation helpers, and the single-attempt comment dispatch. The retrying
phases (parent reparent, the scalar edit, label + link dispatch) STAY in ``dispatch_one``
next to the shared ``_call_with_retry`` backoff hub.

This module is a strict LEAF: it imports nothing from ``dispatch_one`` (its only
cross-module reach is the lazy in-function imports of ``rebar``,
``rebar._commands.identity`` and ``rebar_reconciler._loader``), so ``dispatch_one`` can
re-import the three phase functions back without a cycle. ``dispatch_one`` re-exports
``_update_one_apply_reporter`` / ``_update_one_filter_fields`` /
``_update_one_dispatch_comments`` so ``update_one``'s bare-name calls and
``dispatch_one.<phase>`` attribute access are unchanged.
"""

from __future__ import annotations

import sys
import time
import urllib.error
from pathlib import Path

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
_OUTBOUND_BATCH_ALLOWLIST = frozenset({"summary", "description", "assignee", "priority", "status"})


def _jira_account_id_for(local_ref):
    """Resolve a local reporter string (identity id / email) to a Jira accountId via
    rebar core's identity seam (flow layer may import core), or ``None`` on any miss."""
    if not local_ref or not isinstance(local_ref, str):
        return None
    try:
        from rebar._commands import identity as _identity

        return _identity.jira_account_id(local_ref)
    except Exception:  # noqa: BLE001 — best-effort; an unresolvable reporter is a miss
        return None


def _load_alert_store():
    """Lazy-load the sibling alert_store module by file path (the run_differs / fetcher
    pattern) so a file-path-spec-loaded dispatch_one still resolves it."""
    from rebar_reconciler._loader import lazy_load

    return lazy_load("rebar_reconciler.alert_store", "alert_store.py")


def _record_reporter_alert(kind: str, jira_key, reason: str) -> None:
    """Best-effort soft-fail alert for the reporter REST sub-call (264f). Resolves
    repo_root via ``rebar.config.repo_root()`` and appends a record through the
    lazily-loaded alert_store. Fully fail-open — observability never breaks the sync."""
    try:
        import rebar

        repo_root = Path(rebar.config.repo_root())
    except Exception:  # noqa: BLE001 — no store → nothing to record; never break the pass
        return
    try:
        alert_store = _load_alert_store()
        alert_store.append(
            {
                "kind": kind,
                "jira_key": jira_key,
                "field": "reporter",
                "reason": reason,
                "timestamp_ns": time.time_ns(),
            },
            repo_root=repo_root,
        )
    except Exception:  # noqa: BLE001 — best-effort alert write; non-fatal
        pass


def _update_one_apply_reporter(fields, issue_key, client) -> None:
    """Phase: apply the reporter via a dedicated REST sub-call (264f).

    Pops ``reporter`` off ``fields`` BEFORE the allowlist filter (so it never reaches
    the scalar edit and need not be allowlisted), resolves the local reporter string to
    a Jira accountId via the identity seam, and on success routes it through
    ``client.set_reporter(issue_key, account_id)`` (REST PUT reporter.accountId).

    Soft degradation (the sync never hard-fails on reporter, and other fields still
    apply): an unresolvable reporter is SKIPPED with an ``outbound-reporter-unresolved``
    alert; an ``HTTPError`` from ``set_reporter`` (a 4xx = Modify-Reporter not granted)
    is caught and recorded as ``outbound-reporter-not-permitted``, then execution
    continues."""
    if "reporter" not in fields:
        return
    reporter = fields.pop("reporter", None)
    if not reporter:
        return
    account_id = _jira_account_id_for(reporter)
    if account_id is None:
        _record_reporter_alert(
            "outbound-reporter-unresolved",
            issue_key,
            f"reporter {reporter!r} maps to no identity/accountId; skipped",
        )
        return
    try:
        client.set_reporter(issue_key, account_id)
    except urllib.error.HTTPError as exc:
        # 4xx = Modify-Reporter permission not granted (the common case); any HTTP
        # failure on the reporter sub-call degrades softly so the rest of the update
        # (and the pass) still succeeds.
        _record_reporter_alert(
            "outbound-reporter-not-permitted",
            issue_key,
            f"set_reporter HTTP {exc.code}: {exc.reason}",
        )


def _update_one_filter_fields(fields, mutation) -> dict:
    """Phase: log + strip fields ACLI's edit endpoint rejects, return the allowlisted set."""
    _stripped = {k: v for k, v in fields.items() if k not in _OUTBOUND_BATCH_ALLOWLIST}
    if _stripped:
        print(  # noqa: T201
            f"update_one: dropping fields not accepted by ACLI edit "
            f"for {mutation.get('key')}: {sorted(_stripped.keys())}",
            file=sys.stderr,
        )
    return {k: v for k, v in fields.items() if k in _OUTBOUND_BATCH_ALLOWLIST}


def _update_one_dispatch_comments(mutation, client, issue_key, comment_errors) -> tuple[int, int]:
    """Phase: dispatch comment-add sub-ops (in-band capture into comment_errors).
    Returns (computed, applied) counts."""
    _comments_computed = _comments_applied = 0

    comments = mutation.get("comments", []) or []
    if isinstance(comments, list):
        for entry in comments:
            if not isinstance(entry, dict):
                continue
            body = entry.get("body", "")
            if not body:
                continue
            _comments_computed += 1
            try:
                # Story 9622 (D2): single-attempt, no retry (see create-path note).
                client.add_comment(issue_key, body)
                _comments_applied += 1
            except Exception as exc:  # noqa: BLE001 — in-band capture into comment_errors; non-fatal
                # Bug 6afc-20ee-84e5-4dd5: non-fatal, but surface it so the batch
                # outcome no longer reports error=None for a mutation whose
                # comment sub-mutation failed.
                if comment_errors is not None:
                    comment_errors.append(f"add_comment failed: {exc!s}")
                print(  # noqa: T201
                    f"update_one: add_comment failed for {issue_key}: {exc!r}",
                    file=sys.stderr,
                )
    return _comments_computed, _comments_applied
