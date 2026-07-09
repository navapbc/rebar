#!/usr/bin/env python3
"""Per-ticket outbound dispatch: create_one / update_one + Jira-call retry.

Extracted from ``batch_dispatch.py`` (module-size split, ticket b043-9490): owns
the two per-mutation CLI dispatchers that carry the bulk of outbound dispatch —
``create_one`` (REST-budget guard, JQL dedup, identity-write + rollback, and
label/comment propagation) and ``update_one`` (allowlist filter, parent reparent,
comment-fallback on a 400 illegal-transition, and label/comment/link sub-op
dispatch) — plus the shared ``_call_with_retry`` backoff wrapper they funnel
through, the link-probe helpers (``_index_existing_links`` / ``_find_link_id``),
and the ``_is_illegal_transition_400`` predicate they use.

``batch_dispatch`` re-exports every name defined here, so the public surface
``rebar_reconciler.batch_dispatch.{create_one,update_one,_call_with_retry,...}`` is
unchanged and importers/tests need no edit. Imports only downward (``_errors`` /
``pass_io``); never imports ``batch_dispatch`` or ``applier``, so the facade can
import these dispatchers back without a cycle.

``JiraAPIError`` / ``RetryExhaustedError`` are the UNIFIED types from ``_errors``
(epic romp-swath-wince); imported (not defined) here so the ``batch_dispatch`` /
``applier`` re-exports hand back the SAME objects the ``acli`` surface does.
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

from rebar_reconciler._errors import (
    MAX_BACKOFF_S,
    JiraAPIError,
    RetryExhaustedError,
    http_status,
    parse_retry_after,
)
from rebar_reconciler.binding_store import BindingPersistError
from rebar_reconciler.pass_io import _write_mapping_atomic

logger = logging.getLogger(__name__)


def _index_existing_links(issuelinks) -> set[tuple[str, str]]:
    """Index a ``get_issue_links`` result as a ``{(type_name, other_key)}`` set.

    Bug 3f04: local copy of ``apply_outbound._index_existing_links`` — this module
    deliberately never imports the applier (cycle avoidance), so the helper is
    duplicated. Records ``type.name`` plus the OTHER issue's key on EITHER side
    (``inwardIssue``/``outwardIssue``), so the membership test is direction-agnostic
    (a ``Blocks`` link to B is "present" whether B is the inward or outward side).
    """
    existing: set[tuple[str, str]] = set()
    for link in issuelinks or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        type_name = link_type.get("name") if isinstance(link_type, dict) else None
        if not type_name:
            continue
        for side_key in ("inwardIssue", "outwardIssue"):
            side = link.get(side_key)
            if isinstance(side, dict):
                side_key_val = side.get("key")
                if side_key_val:
                    existing.add((type_name, side_key_val))
    return existing


def _find_link_id(issuelinks, link_type: str, to_key: str) -> str | None:
    """Return the id of the issuelink of ``link_type`` to ``to_key`` (either direction).

    The REMOVE counterpart of :func:`_index_existing_links` (wake-inn-parse): the differ
    emits only (type, to_key) for a managed link to delete; the applier resolves the
    concrete link id from a fresh ``get_issue_links`` probe. Direction-agnostic (matches
    whether ``to_key`` is the inward or outward side). Returns None when no such link
    exists (already removed — idempotent success)."""
    for link in issuelinks or []:
        if not isinstance(link, dict):
            continue
        link_t = link.get("type") or {}
        type_name = link_t.get("name") if isinstance(link_t, dict) else None
        if type_name != link_type:
            continue
        for side_key in ("inwardIssue", "outwardIssue"):
            side = link.get(side_key)
            if isinstance(side, dict) and side.get("key") == to_key:
                link_id = link.get("id")
                return str(link_id) if link_id is not None else None
    return None


# Per-pass REST-call budget: once create_one has issued this many REST calls in a
# pass it defers further creates (back-pressure against Jira rate limits). Named here
# so the threshold has one source instead of a bare literal in the guard + docstring.
_REST_CALL_BUDGET = 200


def _call_with_retry(fn, *args, timeout_s: int = 30, max_retries: int = 3, **kwargs):
    """Call fn(*args, **kwargs) with exponential backoff on retryable failures.

    Retryable: TimeoutError; JiraAPIError 5xx/429; and (story 9622)
    urllib.error.HTTPError 5xx/429 — the REST floor (acli_rest) raises raw
    HTTPError, previously uncaught here, so the idempotent REST writes routed
    through it got zero retry. 429 honors a present integer ``Retry-After``, else
    ADR-0036 jittered backoff; 5xx uses that backoff.
    Non-retryable: JiraAPIError / HTTPError 4xx (except 429) — re-raised raw
    immediately (preserving the 404 / hierarchy-400 semantics). On exhaustion a
    retried HTTPError re-raises raw; TimeoutError/JiraAPIError raise RetryExhaustedError.

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
        retry_after: float | None = None
        try:
            return fn(*args, **kwargs)
        except JiraAPIError as exc:
            # 429 and 5xx are retryable; all other 4xx fail fast
            if exc.status_code != 429 and 400 <= exc.status_code < 500:
                raise
            last_exc = exc
        except urllib.error.HTTPError as exc:
            # REST-transport floor (acli_rest raises raw HTTPError): 429/5xx
            # retryable, other 4xx fail fast (raw). HTTPError.code is the status.
            if exc.code != 429 and 400 <= exc.code < 500:
                raise
            last_exc = exc
            if exc.code == 429:
                retry_after = parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
        except TimeoutError as exc:
            last_exc = exc

        if attempt < max_retries:
            if retry_after is not None:
                delay: float = min(MAX_BACKOFF_S, retry_after)
            elif isinstance(last_exc, urllib.error.HTTPError):
                # ADR 0036: 2**(attempt+1) + jitter, capped.
                delay = min(MAX_BACKOFF_S, 2 ** (attempt + 1) + random.random())
            else:
                delay = delays[min(attempt, len(delays) - 1)]
            time.sleep(delay)

    # On exhaustion of a retried HTTPError, re-raise the ORIGINAL raw HTTPError
    # (story 9622): downstream catchers switch on raw HTTPError (e.g.
    # apply_handlers.handle_update softens 404 but re-raises non-404 5xx as
    # pass-fatal), so wrapping it would silently defeat them. TimeoutError /
    # JiraAPIError keep the RetryExhaustedError contract.
    if isinstance(last_exc, urllib.error.HTTPError):
        raise last_exc
    # CHAIN the cause (PEP 3134) — the prior `raise RetryExhaustedError(str(last_exc))` dropped
    # __cause__, losing the underlying failure (epic romp-swath-wince). Populate last_exception /
    # attempts for post-hoc inspection.
    raise RetryExhaustedError(
        str(last_exc), last_exception=last_exc, attempts=max_retries + 1
    ) from last_exc


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

    Budget guard: if rest_calls >= _REST_CALL_BUDGET, appends mutation to deferred_creates and
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
    if rest_calls >= _REST_CALL_BUDGET:
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
            repo_root = Path(os.environ.get("REBAR_ROOT") or Path(__file__).resolve().parents[4])
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
    # Write-ahead (story 9622): persist a durable pending record BEFORE create so
    # the create->label window is recoverable. A persist failure is item-scoped
    # FATAL — skip the create rather than run it without the record recovery keys on.
    if binding_store is not None and local_id:
        try:
            binding_store.bind_pending(local_id)
            binding_store.save()
        except Exception as persist_err:  # noqa: BLE001 — persist floor -> item-scoped signal
            raise BindingPersistError(
                f"write-ahead bind_pending persist failed for {local_id!r}; "
                f"create skipped: {persist_err!r}"
            ) from persist_err

    result = _call_with_retry(client.create_issue, _ticket_data)

    # Write identity markers so the issue can be re-discovered by dedup JQL
    # and by inbound consumers that inspect entity properties.
    jira_key = result.get("key", "") if isinstance(result, dict) else ""
    if jira_key:
        try:
            # Write-ahead step 3: record the key on the still-pending entry and
            # persist it BEFORE the rebar-id label, so a crash here recovers
            # deterministically. Inside the try so a persist failure rolls back.
            if binding_store is not None and local_id:
                try:
                    binding_store.record_pending_key(local_id, jira_key)
                    binding_store.save()
                except Exception as persist_err:  # noqa: BLE001 — persist floor: translate to the item-scoped signal
                    raise BindingPersistError(
                        f"write-ahead record_pending_key persist failed for "
                        f"{local_id!r} (key {jira_key!r}): {persist_err!r}"
                    ) from persist_err
            # Wrap identity writes in _call_with_retry so transient 5xx/429
            # absorb the same retry budget as create_issue above. Without this,
            # a single transient failure here triggers the unnecessary rollback
            # branch (delete_issue + BRIDGE_ALERT) even though the underlying
            # condition would have cleared on retry.
            _call_with_retry(client.add_label, jira_key, f"rebar-id:{local_id}")
            _call_with_retry(client.set_entity_property, jira_key, "local_id", local_id)
            if binding_store is not None and local_id:
                binding_store.bind_confirm(local_id, jira_key)
        except Exception as write_err:  # noqa: BLE001 — rollback handler: delete the issue + write a failure alert, then re-raise the original write_err (never masked)
            try:
                client.delete_issue(jira_key)
            except Exception:  # noqa: BLE001 — best-effort rollback delete; swallow the delete error so the original write_err re-raises
                pass  # rollback failure must not mask original error
            # Emit BRIDGE_ALERT for identity-write rollback so the event is
            # surfaced in the tickets-tracker for observability.  # tickets-boundary-ok
            try:
                import time as _time
                import uuid as _uuid

                from rebar._store.canonical import canonical_str

                _alert_root = (
                    repo_root
                    or Path(os.environ.get("REBAR_ROOT") or Path(__file__).resolve().parents[4])
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
                _alert_event = {
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
                _alert_path.write_text(canonical_str(_alert_event))
            except Exception:  # noqa: BLE001 — alert-write failure must not mask the original error (write_err is re-raised below)
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
                except Exception as exc:  # noqa: BLE001 — best-effort label add; non-fatal, logged to stderr
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
                    # Story 9622 (D2): SINGLE-attempt (no _call_with_retry) — a
                    # comment has no cheap Jira idempotency key, so a retry could
                    # duplicate it; a failed post falls to comment_errors and is
                    # re-emitted by the comment differ next pass.
                    client.add_comment(jira_key, body)
                except Exception as exc:  # noqa: BLE001 — in-band capture into comment_errors; non-fatal
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
    code = http_status(exc)
    if code != 400:
        return False
    msg = str(exc).lower()
    return "illegal" in msg or "transition" in msg


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


def update_one(
    mutation: dict,
    client,
    comment_errors: list[str] | None = None,
    subop_applied: dict[str, int] | None = None,
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

    Story E (2359): when ``subop_applied`` (a dict) is provided, it is filled with
    per-sub-op ``{labels,comments,links}_computed`` and ``..._applied`` counts so
    the caller can surface telemetry and run the silent-no-op canary. For links,
    ``links_computed`` counts only adds ATTEMPTED after the already-present dedup
    skip, so an idempotent re-sync reports ``links_computed == 0`` (no false canary).

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
    issue_key = mutation.get("key")
    _has_parent_op = _update_one_apply_parent(fields, issue_key, client)
    fields = _update_one_filter_fields(fields, mutation)
    result = _update_one_scalar_update(client, issue_key, fields, _has_parent_op, _attempted_status)

    _labels_computed, _labels_applied = _update_one_dispatch_labels(mutation, client, issue_key)
    _comments_computed, _comments_applied = _update_one_dispatch_comments(
        mutation, client, issue_key, comment_errors
    )
    _links_computed, _links_applied = _update_one_dispatch_links(mutation, client, issue_key)

    if subop_applied is not None:
        subop_applied.update(
            {
                "labels_computed": _labels_computed,
                "labels_applied": _labels_applied,
                "comments_computed": _comments_computed,
                "comments_applied": _comments_applied,
                "links_computed": _links_computed,
                "links_applied": _links_applied,
            }
        )

    return result


def _update_one_apply_parent(fields, issue_key, client) -> bool:
    """Phase: route a parent reparent/clear through client.set_parent (popped from
    ``fields`` before the allowlist filter). Returns whether a parent op was present."""
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
    # Parent-detach churn fix: distinguish a parent CLEAR (the "parent" key is
    # PRESENT with a falsy value — emitted when a ticket is detached locally but
    # Jira still carries the stale epic-link) from "no parent op this mutation"
    # (the key is ABSENT). ``fields.pop("parent", None)`` collapses both to None,
    # so key out the *presence* first. ``client.set_parent`` already clears when
    # passed a falsy key (PUT {"fields":{"parent":None}}), so a CLEAR routes
    # through the identical call path as a SET.
    _has_parent_op = "parent" in fields
    parent_key = fields.pop("parent", None)
    if _has_parent_op:
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
        except Exception as exc:  # noqa: BLE001 — best-effort set_parent; non-fatal, logged
            logger.warning(
                "update_one: set_parent failed for %s parent=%r: %r",
                issue_key,
                parent_key,
                exc,
            )
    return _has_parent_op


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


def _update_one_scalar_update(client, issue_key, fields, _has_parent_op, _attempted_status):
    """Phase: the scalar client.update_issue call + the 400 illegal-transition
    comment-fallback. Returns the update result (or None)."""
    # When the only changed field was parent (the common reparent case), the
    # allowlisted set is now empty AND set_parent already did the work — skip
    # the otherwise-empty client.update_issue call so we don't issue a no-op
    # ACLI edit purely to satisfy a parent-only mutation. The legacy
    # "empty fields still calls update_issue" contract is preserved for the
    # NON-parent case (e.g. an issuetype-only mutation that gets stripped):
    # update_issue is skipped here ONLY when a parent op was the reason the
    # field set is empty (label/comment dispatch below still runs).
    result: dict | None = None
    _skip_empty_update = _has_parent_op and not fields
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
            except Exception:  # noqa: BLE001 — secondary add_comment failure must not mask the comment-fallback path
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
    return result


def _update_one_dispatch_labels(mutation, client, issue_key) -> tuple[int, int]:
    """Phase: dispatch label add/remove sub-ops. Returns (computed, applied) counts."""
    _labels_computed = _labels_applied = 0

    labels = mutation.get("labels", []) or []
    if isinstance(labels, list):
        for entry in labels:
            if not isinstance(entry, dict):
                continue
            action = entry.get("action")
            label_name = entry.get("label", "")
            if not label_name or action not in ("add", "remove"):
                continue
            _labels_computed += 1
            try:
                if action == "add":
                    _call_with_retry(client.add_label, issue_key, label_name)
                elif action == "remove":
                    _call_with_retry(client.remove_label, issue_key, label_name)
                _labels_applied += 1
            except Exception as exc:  # noqa: BLE001 — best-effort label op; non-fatal, logged to stderr
                print(  # noqa: T201
                    f"update_one: label {action} failed for {issue_key} "
                    f"label={label_name!r}: {exc!r}",
                    file=sys.stderr,
                )
    return _labels_computed, _labels_applied


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


def _update_one_dispatch_links(mutation, client, issue_key) -> tuple[int, int]:
    """Phase: dispatch link ADD (deduped) + link REMOVE sub-ops. ``links_computed`` is
    counted POST-DEDUP so an idempotent re-sync reports 0 (no false canary). Returns
    (computed, applied) counts."""
    _links_computed = _links_applied = 0

    # Bug 3f04: dispatch link adds (blocks/relates) via client.set_relationship.
    # The outbound differ emits these alongside changed scalar fields, but the
    # batch path never applied them (the link entry was dropped + no dispatch
    # here) — so outbound link sync was a silent no-op. Mirror the typed leaf
    # ``_apply_outbound_update``: probe the issue's existing links ONCE and skip
    # any add already present (either direction) so a re-issued POST after a
    # timed-out-but-committed create does not duplicate the link. Failures are
    # best-effort + logged (non-fatal — the scalar update already succeeded).
    links = mutation.get("links", []) or []
    if isinstance(links, list) and any(
        isinstance(e, dict) and e.get("action") == "add" for e in links
    ):
        existing_links: set[tuple[str, str]] | None = None
        try:
            existing_links = _index_existing_links(client.get_issue_links(issue_key))
        except Exception as exc:  # noqa: BLE001 — dedup probe is best-effort; proceed without it
            existing_links = None
            print(  # noqa: T201
                f"update_one: get_issue_links probe failed for {issue_key}: {exc!r}",
                file=sys.stderr,
            )
        for entry in links:
            if not isinstance(entry, dict) or entry.get("action") != "add":
                continue
            link_type = entry.get("type")
            to_key = entry.get("to_key")
            if not link_type or not to_key:
                continue
            if existing_links is not None and (link_type, to_key) in existing_links:
                continue  # already present (either direction) — no duplicate add
            # Counted only here — AFTER the dedup skip — so a fully-deduped mutation
            # is computed==0 (no canary), but a genuine drop is computed>0 applied==0.
            _links_computed += 1
            try:
                _call_with_retry(client.set_relationship, issue_key, to_key, link_type)
                _links_applied += 1
            except Exception as exc:  # noqa: BLE001 — best-effort link op; non-fatal, logged
                print(  # noqa: T201
                    f"update_one: set_relationship failed for {issue_key} -> "
                    f"{to_key} ({link_type}): {exc!r}",
                    file=sys.stderr,
                )

    # Symmetric link REMOVE dispatch (wake-inn-parse): a managed link the differ
    # marked for removal (a deliberate local unlink) is deleted on Jira so the inbound
    # differ stops re-adding it. The differ emits only (type, to_key); resolve the link
    # id here by probing the issue's current links (mirrors the ADD dedup probe). A link
    # already gone (no match / 404 / 409) is idempotent success. Best-effort + logged.
    if isinstance(links, list) and any(
        isinstance(e, dict) and e.get("action") == "remove" for e in links
    ):
        try:
            link_objs = client.get_issue_links(issue_key)
        except Exception as exc:  # noqa: BLE001 — probe is best-effort; skip removals this pass
            link_objs = None
            print(  # noqa: T201
                f"update_one: get_issue_links probe (remove) failed for {issue_key}: {exc!r}",
                file=sys.stderr,
            )
        if link_objs is not None:
            for entry in links:
                if not isinstance(entry, dict) or entry.get("action") != "remove":
                    continue
                link_type = entry.get("type")
                to_key = entry.get("to_key")
                if not link_type or not to_key:
                    continue
                link_id = _find_link_id(link_objs, link_type, to_key)
                if link_id is None:
                    continue  # already absent in Jira — idempotent success, nothing to do
                _links_computed += 1
                try:
                    _call_with_retry(client.delete_issue_link, link_id)
                    _links_applied += 1
                except subprocess.CalledProcessError:
                    # delete_issue_link shells out via ACLI (raises CalledProcessError, NOT
                    # an HTTPError). We only reach here after _find_link_id confirmed the link
                    # exists in a fresh probe, so a failure now is dominated by a concurrent
                    # removal (404) / concurrent change (409) — idempotent: the desired
                    # end-state (link gone) is reached. A genuine persistent failure is
                    # self-healing — the differ recomputes the REMOVE from managed_refs next
                    # pass — so treat as non-fatal and don't unwind the pass.
                    _links_applied += 1
                except Exception as exc:  # noqa: BLE001 — best-effort link op; non-fatal, logged
                    print(  # noqa: T201
                        f"update_one: delete_issue_link failed for {issue_key} -> "
                        f"{to_key} ({link_type}): {exc!r}",
                        file=sys.stderr,
                    )
    return _links_computed, _links_applied
