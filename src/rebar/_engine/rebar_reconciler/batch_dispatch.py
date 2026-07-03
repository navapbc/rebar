#!/usr/bin/env python3
"""Outbound batch-dispatch facade: delete_one + mutation->batch-dict + re-exports.

The bulk of per-mutation dispatch — ``create_one`` / ``update_one`` plus the
``_call_with_retry`` backoff wrapper, the link-probe helpers, and the
``_is_illegal_transition_400`` predicate — was extracted to the sibling
``dispatch_one.py`` (module-size split, ticket b043-9490) and is re-exported here
so this module stays the stable public surface (``rebar_reconciler.batch_dispatch.*``)
that ``applier`` / ``apply_handlers`` / ``apply_outbound`` / ``apply_inbound`` and
the reconciler tests import from.

This module still OWNS ``delete_one`` (close = delete the Jira issue, 404
tolerated) and ``_mutation_to_batch_dict`` (normalise a typed Mutation into the
legacy batch-dict shape). Imports only downward (``_errors``, ``dispatch_one``);
never imports ``applier``, so the orchestrator can import these back without a
cycle.
"""

from __future__ import annotations

from rebar_reconciler._errors import (
    JiraAPIError,
    RetryExhaustedError,
    is_not_found,
)
from rebar_reconciler.dispatch_one import (
    _call_with_retry,
    _find_link_id,
    _index_existing_links,
    _is_illegal_transition_400,
    create_one,
    update_one,
)

__all__ = [
    "JiraAPIError",
    "RetryExhaustedError",
    "_call_with_retry",
    "_find_link_id",
    "_index_existing_links",
    "_is_illegal_transition_400",
    "_mutation_to_batch_dict",
    "create_one",
    "delete_one",
    "update_one",
]


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
        if is_not_found(exc):
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
        # Surface links so update_one can dispatch them via set_relationship
        # (bug 3f04). Previously omitted here, so the production batch path
        # silently dropped every outbound blocks/relates link — the link was
        # reported "applied" (the mutation succeeded) but never created in Jira.
        "links": payload.get("links", []),
    }
