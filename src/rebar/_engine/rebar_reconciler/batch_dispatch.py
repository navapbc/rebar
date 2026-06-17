#!/usr/bin/env python3
"""Outbound batch mutation execution + resilient Jira-call retry.

Owns the per-mutation CLI dispatchers (``create_one``/``update_one``/
``delete_one``) that the legacy batch path runs against a live ``AcliClient``,
the ``_call_with_retry`` backoff wrapper they all funnel through (with its
``JiraAPIError``/``RetryExhaustedError`` types), and ``_mutation_to_batch_dict``
which normalises a typed Mutation into the legacy batch-dict shape.

Imports only downward (pass_io); never imports applier, so the orchestrator can
import the dispatchers back without a cycle.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
from pathlib import Path

from rebar_reconciler.pass_io import _write_mapping_atomic

logger = logging.getLogger(__name__)


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
            repo_root = Path(
                os.environ.get("REBAR_ROOT")
                or Path(__file__).resolve().parents[4]
            )
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
            _call_with_retry(client.set_entity_property, jira_key, "local_id", local_id)
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
                import json as _json
                import time as _time
                import uuid as _uuid

                _alert_root = (
                    repo_root
                    or Path(
                        os.environ.get("REBAR_ROOT")
                        or Path(__file__).resolve().parents[4]
                    )
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


def update_one(mutation: dict, client, comment_errors: list[str] | None = None) -> dict | None:
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
