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
import sys

# ``_call_with_retry`` moved to dispatch_apply_phases (ticket a3fa, to make room for the
# capability guards under the 800-LOC module cap), but ``dispatch_one.time`` is a DOCUMENTED
# retry patch point: test_error_taxonomy.py:61 and test_cloud_rate_limit_sweep_heldout.py:259
# reach for it as ``monkeypatch.setattr(dispatch_one.time, "sleep", ...)``. That mutates the
# SHARED ``time`` module object, so it still suppresses the relocated backoff — but only while
# the attribute resolves here. Kept deliberately; do not drop as "unused".
import time  # noqa: F401
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# Non-retrying update_one apply phases live in the sibling leaf (module-size split,
# ticket 744a); the two pure link-probe helpers joined them there (ticket cc77) to
# make room for this module's port annotations. Re-imported here so update_one's
# bare-name calls and ``dispatch_one.<name>`` attribute access resolve unchanged.
from rebar_reconciler._backend import SupportsComments
from rebar_reconciler._errors import (
    JiraAPIError,
    RetryExhaustedError,
    http_status,
)
from rebar_reconciler.binding_store import BindingPersistError
from rebar_reconciler.dispatch_apply_phases import (
    _call_with_retry,
    _capability_present,
    # Re-exports: the link-probe helpers no longer have a caller in THIS module (the link
    # phase moved to the sibling leaf, ticket 5528), but ``batch_dispatch`` imports them
    # from here and its __all__ is what the reconciler tests reach for.
    _find_link_id,  # noqa: F401 — re-export for batch_dispatch
    _index_existing_links,  # noqa: F401 — re-export for batch_dispatch
    _record_comment_id,
    _update_one_apply_reporter,
    _update_one_dispatch_comments,
    _update_one_dispatch_links,
    _update_one_filter_fields,
)
from rebar_reconciler.pass_io import _write_mapping_atomic, record_parent_divergence
from rebar_reconciler.transition_replay import (
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
    create_core: Callable[..., dict | None] | None = None,
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
    # MIDDLE core (S4 T3 cutover, REB-3115): route the create->contain write-ahead
    # sequence through the SELECTED core. ``create_core`` is the coordinated composition
    # injected by ``apply_handlers.handle_create`` when the ``create_route`` selector is
    # the coordinator; ``None`` selects the legacy write-ahead core (the rollback value).
    # EXACTLY ONE runs, never both (AC6, no dual-send). A boolean ``or`` selection adds
    # ZERO McCabe branch here. Both cores share the same signature, RAISE on failure
    # exactly as the inline legacy code did, and return the create-response dict on
    # success; a ``None`` return is the coordinated ``commit_unknown`` DEFER (the mutation
    # was appended to ``deferred_creates`` by the injected core) — the postlude below
    # skips (``jira_key`` is empty) and ``return result`` hands back None, mirroring the
    # budget-defer contract.
    core = create_core or _legacy_create_core
    result = core(
        local_id, _ticket_data, client=client, binding_store=binding_store, repo_root=repo_root
    )
    jira_key = result.get("key", "") if isinstance(result, dict) else ""

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
                    print(
                        f"create_one: add_label failed for {jira_key} "
                        f"label={label_name!r}: {exc!r}",
                        file=sys.stderr,
                    )

        comments = mutation.get("comments", []) or []
        # Ticket a3fa: the capability guard is folded INTO this condition rather than placed
        # inside the loop. Two reasons: the capability is invariant across the loop (checking
        # per entry would re-decide the same thing N times), and a boolean operand costs zero
        # McCabe complexity, so the guard does not raise create_one's LOCKED complexity
        # baseline. It fires only when the mutation actually carries comment sub-ops.
        # A designed skip is NOT a failure: it does not reach comment_errors (that channel
        # stays the failure channel) and the create itself still lands.
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
                # Story 4cee: send the bytes the dedup key was taken from. An
                # entry queued before the cutover — and every entry
                # ``_map_comments_for_create`` emits — carries no ``wire_body``,
                # so the unfitted body remains the fallback and the adapter's own
                # fit still covers it.
                body = entry.get("wire_body") or entry.get("body", "")
                if not body:
                    continue
                try:
                    # Story 9622 (D2): SINGLE-attempt (no _call_with_retry) — a
                    # comment has no cheap Jira idempotency key, so a retry could
                    # duplicate it; a failed post falls to comment_errors and is
                    # re-emitted by the comment differ next pass.
                    _comment_result = cast("SupportsComments", client).add_comment(jira_key, body)
                    # emersed-specific-mutt: capture the returned Jira comment ID and
                    # persist it against the entry's local_comment_key (HLC) via the
                    # binding_store's write-ahead map, so a re-sync never re-posts it.
                    _record_comment_id(binding_store, entry, _comment_result)
                except Exception as exc:  # noqa: BLE001 — in-band capture into comment_errors; non-fatal
                    # Bug ea6d-e4b2-a316-45ec: non-fatal, but surface it so the
                    # batch outcome no longer reports error=None for an outbound
                    # CREATE whose comment sub-mutation failed. Mirrors update_one.
                    if comment_errors is not None:
                        comment_errors.append(f"add_comment failed: {exc!s}")
                    print(
                        f"create_one: add_comment failed for {jira_key}: {exc!r}",
                        file=sys.stderr,
                    )

    return result


def _emit_create_identity_alert(local_id: str, jira_key: str, repo_root: Path | None) -> None:
    """Emit a BRIDGE_ALERT recording an identity-write failure AFTER a create landed.

    Extracted verbatim from ``create_one``'s post-create rollback branch (bug 387d /
    ticket 021d). The created Jira issue is NEVER deleted: the write-ahead protocol has
    already recorded the key (keyed-pending), so recovery retro-attaches the remaining
    label/property deterministically with no Jira search. This helper only publishes the
    observability alert; the caller re-raises the original write error. Shared by the
    legacy write-ahead core here and the coordinated core in ``create_route`` so both
    routes emit byte-identical alerts. An alert-write failure is swallowed so it can
    never mask the original error.
    """
    try:
        import time as _time
        import uuid as _uuid

        from rebar._store import staging as _staging
        from rebar._store.canonical import canonical_str
        from rebar.config import reconciler_repo_root as _owned_repo_root

        _alert_root = (repo_root or _owned_repo_root()) / ".tickets-tracker"
        # F7: defensive guard — if local_id is falsy the alert directory would resolve to
        # .tickets-tracker root and pollute it. Prefer the jira_key, falling back to a uuid
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


def _legacy_create_core(
    local_id: str,
    ticket_data: dict,
    *,
    client: TicketTransport,
    binding_store=None,
    repo_root: Path | None = None,
) -> dict:
    """The legacy write-ahead create->contain MIDDLE core (the rollback value).

    Extracted verbatim from ``create_one`` so its host drops below its frozen McCabe
    ceiling and the coordinated route can slot an alternate core into the same seam.
    Sequence (story 9622 / bug 387d): bind_pending+save → create_issue (via
    ``_call_with_retry``) → record_pending_key(+record_jira_id)+save → add_label →
    set_entity_property → bind_confirm. RAISES exactly as the inline code did —
    ``BindingPersistError`` on a persist-floor failure, and on a post-create identity
    write failure it emits the BRIDGE_ALERT (NEVER deleting the issue) and re-raises the
    original error. Returns the ``create_issue`` response dict on success.
    """
    if binding_store is not None and local_id:
        try:
            binding_store.bind_pending(local_id)
            binding_store.save()
        except Exception as persist_err:
            raise BindingPersistError(
                f"write-ahead bind_pending persist failed for {local_id!r}; "
                f"create skipped: {persist_err!r}"
            ) from persist_err

    result = _call_with_retry(client.create_issue, ticket_data)

    jira_key = result.get("key", "") if isinstance(result, dict) else ""
    if jira_key:
        try:
            if binding_store is not None and local_id:
                _legacy_record_key(binding_store, local_id, jira_key, result)
            _call_with_retry(client.add_label, jira_key, f"rebar-id:{local_id}")
            _call_with_retry(client.set_entity_property, jira_key, "local_id", local_id)
            if binding_store is not None and local_id:
                binding_store.bind_confirm(local_id, jira_key)
        except Exception as write_err:
            _emit_create_identity_alert(local_id, jira_key, repo_root)
            raise write_err
    return result


def _legacy_record_key(binding_store, local_id: str, jira_key: str, result) -> None:
    """Write-ahead step 3: record the key (and immutable id) on the still-pending entry.

    Persisted BEFORE the rebar-id label so a crash recovers deterministically. Raises
    ``BindingPersistError`` on a persist failure so the caller's rollback branch runs.
    """
    try:
        binding_store.record_pending_key(local_id, jira_key)
        # Capture the IMMUTABLE numeric id in the SAME write (bug 7c26). A project move
        # re-keys the issue; the id is the only handle that survives it, and this create
        # response is where it is known. getattr-guarded so a store predating it is a
        # no-op, not a persist-floor failure that would skip the create.
        _record_id = getattr(binding_store, "record_jira_id", None)
        if _record_id is not None:
            _record_id(local_id, result.get("id", ""))
        binding_store.save()
    except Exception as persist_err:
        raise BindingPersistError(
            f"write-ahead record_pending_key persist failed for "
            f"{local_id!r} (key {jira_key!r}): {persist_err!r}"
        ) from persist_err


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

    Bug e6e9: ``fields_synced`` collects the fields the scalar ``update_issue`` call
    ACTUALLY landed — empty on every non-success arm."""
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
            # Hierarchy guard (ticket 8b25): only an Epic may be a parent; a
            # Task→Task reparent (and any other unmet hierarchy constraint) is
            # rejected with HTTP 400 carrying a misleading "same project"
            # message. The guard is NOT vendor-conditional — Data Center reaches
            # the same verdict by accepting the write and silently ignoring it
            # (capability map req-0056/req-0058), which is worse, not better.
            # Treat any 400 as a hierarchy rejection: WARN + continue. Non-400
            # errors stay non-fatal too — a parent failure must not abort the
            # rest of the batch.
            #
            # Every arm ALSO records a bridge_alerts entry (ticket 39c1 AC4).
            # Warn-and-continue alone is why "the parent never reached the
            # tracker" stayed invisible through five instances of this class:
            # the pass exits 0 and nothing durable reports the divergence.
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
    """Phase: the scalar client.update_issue call + the 400 illegal-transition
    comment-fallback. Returns the update result (or None).

    ``assignee_is_account_id`` (264f) is forwarded to ``client.update_issue`` so that
    when the assignee is an already-resolved accountId (identity fast path) acli submits
    it directly and skips the assignable search.

    Returns ``(result, applied_fields)``; ``applied_fields`` (bug e6e9) is assigned on
    exactly ONE line below, so it stays EMPTY unless ``client.update_issue`` completed.
    Contract: ``apply_handlers.handle_update``; rationale: ``peer_state.merge_baseline``."""
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
            result = _call_with_retry(client.update_issue, issue_key, **_extra, **fields)
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
    print(log_entry, file=sys.stderr)
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
                    _call_with_retry(client.add_label, issue_key, label_name)
                elif action == "remove":
                    _call_with_retry(client.remove_label, issue_key, label_name)
                _labels_applied += 1
            except Exception as exc:  # noqa: BLE001 — best-effort label op; non-fatal, logged to stderr
                print(
                    f"update_one: label {action} failed for {issue_key} "
                    f"label={label_name!r}: {exc!r}",
                    file=sys.stderr,
                )
    return _labels_computed, _labels_applied
