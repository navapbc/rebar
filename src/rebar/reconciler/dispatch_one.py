#!/usr/bin/env python3
"""Per-ticket outbound dispatch: create_one / update_one, one attempt per Jira call.

Each Jira call is a single attempt. Transports own retries. create_one guards
the call budget, deduplicates by identity label, and delegates the write-ahead
create route. update_one sequences parent, scalar, label, comment, and link
sub-operations. batch_dispatch re-exports these entry points and error types."""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# Non-retrying update_one apply phases live in the sibling leaf (module-size split,
# ticket 744a); the two pure link-probe helpers joined them there (ticket cc77) to
# make room for this module's port annotations. Re-imported here so update_one's
# bare-name calls and ``dispatch_one.<name>`` attribute access resolve unchanged.
from rebar.reconciler._backend import SupportsComments
from rebar.reconciler._errors import (
    JiraAPIError,
    RetryExhaustedError,
    http_status,
)
from rebar.reconciler.dispatch_apply_phases import (
    _capability_present,
    # Keep these link-probe exports for batch_dispatch and its callers.
    _find_link_id,  # noqa: F401 — re-export for batch_dispatch
    _index_existing_links,  # noqa: F401 — re-export for batch_dispatch
    _record_comment_id,
    _update_one_apply_reporter,
    _update_one_dispatch_comments,
    _update_one_dispatch_links,
    _update_one_filter_fields,
)
from rebar.reconciler.pass_io import _write_mapping_atomic, record_parent_divergence
from rebar.reconciler.scratch_retention import mapping_path as _scratch_mapping_path
from rebar.reconciler.transition_replay import (
    recorded_hops_for,
    replay_should_drift,
    replay_transition,
)

if TYPE_CHECKING:
    from ._backend import SupportsComments, TicketTransport

logger = logging.getLogger(__name__)


# Per-pass REST-call budget: once create_one has issued this many REST calls in a
# pass it defers further creates (back-pressure against Jira rate limits). Named here
# so the threshold has one source instead of a bare literal in the guard + docstring.
_REST_CALL_BUDGET = 200


def create_one(
    mutation: dict,
    client: TicketTransport,
    rest_calls: int = 0,
    deferred_creates: list | None = None,
    events_list: list | None = None,
    repo_root: Path | None = None,
    binding_store=None,
    comment_errors: list[str] | None = None,
    *,
    create_core: Callable[..., dict | None],
) -> dict | None:
    """Create one Jira issue within the REST budget, or defer it without a call.
    A keyless pending binding within index-lag grace also defers. Search the rebar-id
    label first and confirm an existing issue instead of creating a duplicate.
    Otherwise translate bridge fields, run one selected write-ahead core, then send
    user labels and comments. Return the create result, dedup sentinel, or None
    when deferred."""
    # Budget guard: defer without any REST call when at or over the limit
    if rest_calls >= _REST_CALL_BUDGET:
        if deferred_creates is not None:
            deferred_creates.append(mutation)
        return None

    local_id = mutation.get("local_id", "")

    # DEFER while a KEYLESS-PENDING binding is inside the index-lag grace window (21fc).
    #
    # The differ reaches the create branch on `get_jira_key(...) is None`, which is ALSO
    # true of a keyless-pending entry — the state entered precisely when a previous pass
    # crashed DURING create_issue. On DC the Lucene index is eventually consistent
    # (JRASERVER-70423: a 2,991s lag observed), so the dedup search below misses an issue
    # that really exists and we write a SECOND one. Deferring here rather than at plan
    # time guards the actual WRITE, and reuses the budget path's existing semantics: the
    # mutation is deferred, not dropped. Once the index catches up,
    # `recover_pending_bindings` binds to the existing issue; if the create truly never
    # landed, the grace window expires and this proceeds normally.
    if binding_store is not None and local_id:
        within_grace = getattr(binding_store, "is_keyless_pending_within_grace", None)
        if within_grace is not None and within_grace(local_id):
            if deferred_creates is not None:
                deferred_creates.append(mutation)
            return None

    jql = f'labels = "rebar-id:{local_id}"'
    hits = client.search_issues(jql)

    if hits:
        hit_key = hits[0].get("key", "")

        # Persist local_id -> jira_key in mapping.json atomically
        if repo_root is None:
            from rebar.config import reconciler_repo_root as _owned_repo_root

            repo_root = _owned_repo_root()
        _write_mapping_atomic(_scratch_mapping_path(repo_root), local_id, hit_key)

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
    # MIDDLE core (S4 T3 cutover, REB-3115): run the create->contain write-ahead
    # sequence through the injected ``create_core`` — the coordinated composition
    # ``apply_handlers.handle_create`` injects (EXACTLY ONE route, no dual-send, AC6).
    # The core RAISES on failure and returns the create-response dict on success; a
    # ``None`` return is the coordinated ``commit_unknown`` DEFER (the mutation was
    # appended to ``deferred_creates`` by the injected core) — the postlude below skips
    # (``jira_key`` is empty) and ``return result`` hands back None, mirroring the
    # budget-defer contract.
    result = create_core(
        local_id, _ticket_data, client=client, binding_store=binding_store, repo_root=repo_root
    )
    jira_key = result.get("key", "") if isinstance(result, dict) else ""

    # After identity is durable, send user labels and comments. Their failures are
    # reported without rolling back the created issue.
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
                    client.add_label(jira_key, label_name)
                except Exception as exc:  # noqa: BLE001 — best-effort label add; non-fatal, logged to stderr
                    print(  # noqa: T201
                        f"create_one: add_label failed for {jira_key} "
                        f"label={label_name!r}: {exc!r}",
                        file=sys.stderr,
                    )

        comments = mutation.get("comments", []) or []
        # Check comment capability once when comments exist. A designed skip leaves
        # comment_errors for failed calls while the create remains successful.
        if (
            isinstance(comments, list)
            and comments
            and _capability_present(
                client, SupportsComments, "comments", "add_comment", "create_one", jira_key
            )
        ):
            for entry in comments:
                if not isinstance(entry, dict):
                    continue
                # Send the wire body used for deduplication. Older entries fall back to body.
                body = entry.get("wire_body") or entry.get("body", "")
                if not body:
                    continue
                try:
                    # Post comments once because retrying without a Jira idempotency key can
                    # duplicate them. The next pass handles a failed post.
                    _comment_result = cast("SupportsComments", client).add_comment(jira_key, body)
                    # Record local comment identity after a landed post to prevent replay.
                    _record_comment_id(binding_store, entry, _comment_result)
                except Exception as exc:  # noqa: BLE001 — in-band capture into comment_errors; non-fatal
                    # Keep a nonfatal comment failure visible in the batch outcome.
                    if comment_errors is not None:
                        comment_errors.append(f"add_comment failed: {exc!s}")
                    print(  # noqa: T201
                        f"create_one: add_comment failed for {jira_key}: {exc!r}",
                        file=sys.stderr,
                    )

    return result


def _emit_create_identity_alert(local_id: str, jira_key: str, repo_root: Path | None) -> None:
    """Record an identity-write failure after Jira create without deleting the issue.
    The keyed pending binding supports deterministic recovery. Alert-write failure
    must not mask the original error."""
    try:
        import time as _time
        import uuid as _uuid

        from rebar._store import staging as _staging
        from rebar._store.canonical import canonical_str
        from rebar.config import reconciler_repo_root as _owned_repo_root
        from rebar.config import tracker_dir as _resolve_store

        # RESOLVED, not composed: the store is RELOCATABLE, so a relocated deployment must
        # get its alert written into the CONFIGURED store, not an unconfigured repo-root dir.
        _alert_root = _resolve_store(repo_root or _owned_repo_root())
        # F7: defensive guard — if local_id is falsy the alert directory would resolve to
        # the store root and pollute it. Prefer the jira_key, falling back to a uuid
        # so the alert always lands under a non-root subdirectory.
        _alert_dir_key = local_id or jira_key or f"unknown-{_uuid.uuid4()}"
        _ts = _time.time_ns()
        _alert_uuid = str(_uuid.uuid4())
        _alert_name = f"{_ts}-{_alert_uuid}-BRIDGE_ALERT.json"
        _alert_event = {
            "event_type": "BRIDGE_ALERT",
            "timestamp": _ts,
            "uuid": _alert_uuid,
            "ticket_id": local_id,
            "jira_key": jira_key,
            "data": {
                "reason": (
                    "identity-write failed after create; Jira issue RETAINED and "
                    "left keyed-pending for retro-attach on the next pass"
                ),
                "tag": "create-identity-write-failed",
            },
        }
        # Ticket 021d: publish the directory and this first alert together with one rename,
        # so an interruption never leaves an empty directory fsck reports as MISSING_CREATE
        # + FOREIGN_STORE_PATH.
        _staged = _staging.stage_event(
            str(_alert_root),
            _alert_dir_key,
            _alert_name,
            canonical_str(_alert_event).encode("utf-8"),
        )
        try:
            _staged.promote()
        finally:
            _staged.discard()  # no-op once published
    except Exception:  # noqa: BLE001 — alert-write failure must not mask the original error
        pass  # alert write failure must not mask original error


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


def update_one(
    mutation: dict,
    client: TicketTransport,
    comment_errors: list[str] | None = None,
    subop_applied: dict[str, int] | None = None,
    fields_synced: dict[str, Any] | None = None,
    link_confirm: Callable[..., None] | None = None,
    binding_store=None,
) -> dict | None:
    """Update an issue through parent, scalar, label, comment, and link phases.
    Comment and link failures are nonfatal but reach their error sinks and sub-operation
    counts. These counts distinguish a designed capability skip from a silent no-op.
    The scalar phase returns only fields whose Jira edit completed for baseline merge."""
    fields = mutation.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    # Capture pre-filter status so the comment-fallback path (which reads
    # ``fields.get("status")`` after the allowlist strips it) can still
    # report the attempted local status (bug 85a1 follow-up).
    _attempted_status = fields.get("status")
    issue_key = mutation.get("key")
    # 264f: the accountId fast-path sentinel rides INSIDE the fields dict (the frozen
    # OutboundMutation has no field for it). Pop it here — before the allowlist filter —
    # and forward it as a bool kwarg so acli submits the resolved accountId directly.
    _assignee_is_account_id = bool(fields.pop("_assignee_is_account_id", False))
    _has_parent_op = _update_one_apply_parent(fields, issue_key, client, mutation.get("local_id"))
    _update_one_apply_reporter(fields, issue_key, client)
    fields = _update_one_filter_fields(fields, mutation)
    result, _applied = _update_one_scalar_update(
        client,
        issue_key,
        fields,
        _has_parent_op,
        _attempted_status,
        _assignee_is_account_id,
        mutation.get("local_id"),
    )
    if fields_synced is not None:
        fields_synced.update(_applied)

    _labels_computed, _labels_applied = _update_one_dispatch_labels(mutation, client, issue_key)
    _comments_computed, _comments_applied = _update_one_dispatch_comments(
        mutation, client, issue_key, comment_errors, binding_store=binding_store
    )
    _links_computed, _links_applied, _links_failed = _update_one_dispatch_links(
        mutation, client, issue_key, link_confirm=link_confirm
    )

    if subop_applied is not None:
        subop_applied.update(
            {
                "labels_computed": _labels_computed,
                "labels_applied": _labels_applied,
                "comments_computed": _comments_computed,
                "comments_applied": _comments_applied,
                "links_computed": _links_computed,
                "links_applied": _links_applied,
                "links_failed": _links_failed,
            }
        )

    return result


def _update_one_apply_parent(
    fields, issue_key, client: TicketTransport, local_id: str | None = None
) -> bool:
    """Phase: route a parent reparent/clear through client.set_parent (popped from
    ``fields`` before the allowlist filter). Returns whether a parent op was present."""
    # A present parent key may request SET or CLEAR. Remove it before the scalar
    # allowlist and use set_parent for both values. Absence means no parent operation.
    _has_parent_op = "parent" in fields
    parent_key = fields.pop("parent", None)
    if _has_parent_op:
        try:
            client.set_parent(issue_key, parent_key)
        except urllib.error.HTTPError as exc:
            # Treat HTTP 400 as a hierarchy rejection. Continue the batch, but record the
            # parent divergence in bridge alerts. Other parent failures remain visible too.
            if exc.code == 400:
                logger.warning(
                    "parent sync skipped: Jira hierarchy rejected %s->%s (HTTP 400)",
                    issue_key,
                    parent_key,
                )
                _alert_kind = "outbound-parent-rejected"
            else:
                logger.warning(
                    "update_one: set_parent failed for %s parent=%r: %r",
                    issue_key,
                    parent_key,
                    exc,
                )
                _alert_kind = "outbound-parent-failed"
            record_parent_divergence(_alert_kind, issue_key, local_id, parent_key, exc)
        except NotImplementedError as exc:
            # The transport says this deployment has no way to express the
            # relationship at all — distinct from Jira refusing THIS parent,
            # because no retry and no different parent shape will help.
            logger.warning(
                "update_one: parent unrepresentable for %s parent=%r: %r",
                issue_key,
                parent_key,
                exc,
            )
            record_parent_divergence(
                "outbound-parent-unrepresentable", issue_key, local_id, parent_key, exc
            )
        except Exception as exc:  # noqa: BLE001 — best-effort set_parent; non-fatal, logged
            logger.warning(
                "update_one: set_parent failed for %s parent=%r: %r",
                issue_key,
                parent_key,
                exc,
            )
            record_parent_divergence("outbound-parent-failed", issue_key, local_id, parent_key, exc)
    return _has_parent_op


def _update_one_scalar_update(
    client: TicketTransport,
    issue_key,
    fields,
    _has_parent_op,
    _attempted_status,
    assignee_is_account_id=False,
    local_id: str | None = None,
):
    """Run the scalar edit and handle an illegal-transition 400 with its comment fallback.
    Forward assignee_is_account_id only for a resolved account ID. Return the edit
    result and applied fields, keeping applied fields empty if the edit failed."""
    # When the only changed field was parent (the common reparent case), the
    # allowlisted set is now empty AND set_parent already did the work — skip
    # the otherwise-empty client.update_issue call so we don't issue a no-op
    # ACLI edit purely to satisfy a parent-only mutation. The legacy
    # "empty fields still calls update_issue" contract is preserved for the
    # NON-parent case (e.g. an issuetype-only mutation that gets stripped):
    # update_issue is skipped here ONLY when a parent op was the reason the
    # field set is empty (label/comment dispatch below still runs).
    result: dict | None = None
    applied: dict[str, Any] = {}
    _skip_empty_update = _has_parent_op and not fields
    if _skip_empty_update:
        pass  # parent handled via set_parent; no scalar fields to edit
    else:
        try:
            # 264f: only forward the flag when True — the default (False) is omitted so
            # the common update carries exactly its mapped fields (no bogus kwarg leaks
            # into stubs / the ACLI boundary).
            _extra = {"assignee_is_account_id": True} if assignee_is_account_id else {}
            result = client.update_issue(issue_key, **_extra, **fields)
            applied = dict(fields)  # reached ONLY on a completed write
        except RetryExhaustedError:
            # A genuinely transient failure (5xx/429/timeout) that EXHAUSTED its retries is
            # NOT a workflow transition rejection — it is only a ``RuntimeError`` subclass
            # by MRO. Re-raise it AHEAD of the widened catch below so it propagates to the
            # pass's soft-fail backstop instead of being misrouted into replay/comment/drift.
            raise
        except (RuntimeError, ValueError, urllib.error.HTTPError, JiraAPIError) as exc:
            # S6: a workflow that forbids the DIRECT end-state hop surfaces here as one of
            # the transition-rejection shapes both backends raise — acli ``RuntimeError``,
            # DC ``ValueError`` / ``IllegalTransitionError``, or an illegal-transition 400
            # (``HTTPError`` / ``BackendHTTPError`` / ``JiraAPIError``). Replay walks rebar's
            # OWN recorded intermediate hops; if it reaches the target the update lands.
            #   * A NON-transition HTTP/API error (a non-illegal 400, a 404 stale binding, a
            #     5xx) is NOT a rejection: it propagates unchanged to its upstream handler.
            #   * If replay cannot reach the target, ``replay_should_drift`` decides between
            #     the legacy comment-fallback DRIFT (illegal-400, no local_id, or a recorded
            #     trail existed) and PROPAGATING the original error to the pre-S6 soft-fail
            #     backstop (a bare transition error with no recorded trail to replay).
            if _is_non_transition_http_error(exc):
                raise
            # Read the recorded hop trail ONCE and thread it into BOTH replay helpers, so
            # the drift decision does not re-glob/re-parse the store a second time in this
            # same arm (avoids duplicate IO and a TOCTOU window).
            _hops = recorded_hops_for(local_id)
            if replay_transition(client, issue_key, local_id, _attempted_status, hops=_hops):
                result = {"key": issue_key}
                applied = dict(fields)
            elif _is_illegal_transition_400(exc) or replay_should_drift(local_id, hops=_hops):
                result = _scalar_update_comment_fallback(client, issue_key, _attempted_status)
            else:
                raise
    return result, applied


def _is_non_transition_http_error(exc: Exception) -> bool:
    """True for an HTTP/API error that is NOT an illegal-transition 400 — a non-illegal 400
    (e.g. a missing required field), a 404 stale binding, or a 5xx. Such errors are not
    workflow rejections and must propagate to their upstream handler rather than route into
    the S6 replay/comment-fallback path. ``RuntimeError`` / ``ValueError`` (acli / DC
    transition rejections) are never HTTP-like, so they always route to replay."""
    if not isinstance(exc, (urllib.error.HTTPError, JiraAPIError)):
        return False
    return not _is_illegal_transition_400(exc)


def _scalar_update_comment_fallback(client: TicketTransport, issue_key, _attempted_status):
    """Legacy 400-illegal-transition drift arm: record the local status change as a
    Jira comment (capability-gated) + a structured stderr log, and return ``None`` so
    the mutation drifts. Reached only when replay could not walk to the target."""
    new_status = _attempted_status
    comment = f"local status changed to {new_status}"
    if _capability_present(
        client,
        SupportsComments,
        "comments",
        "add_comment",
        "update_one.scalar_update_fallback",
        issue_key,
    ):
        try:
            cast("SupportsComments", client).add_comment(issue_key, comment)
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
    print(log_entry, file=sys.stderr)  # noqa: T201
    return None


def _update_one_dispatch_labels(mutation, client: TicketTransport, issue_key) -> tuple[int, int]:
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
                    client.add_label(issue_key, label_name)
                elif action == "remove":
                    client.remove_label(issue_key, label_name)
                _labels_applied += 1
            except Exception as exc:  # noqa: BLE001 — best-effort label op; non-fatal, logged to stderr
                print(  # noqa: T201
                    f"update_one: label {action} failed for {issue_key} "
                    f"label={label_name!r}: {exc!r}",
                    file=sys.stderr,
                )
    return _labels_computed, _labels_applied
