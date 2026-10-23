#!/usr/bin/env python3
"""Turn inbound Jira records into local reducer event files.

Create/update helpers write CREATE, STATUS, EDIT, TAG_DELTA, and COMMENT events.
Identity and link operations live in ``apply_inbound_records`` and are imported
only at call time where needed, avoiding a module import cycle. Update helpers
append paths to the caller's ``written`` list.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._backend import TicketTransport

from rebar.reconciler import projects_store
from rebar.reconciler.inbound_translate import (
    _BRIDGE_INTERNAL_TAG_PREFIXES,
    _JIRA_TYPE_MAP,
    _extract_name,
    _jira_key_to_local_id,
    _jira_status_to_local,
    _load_ticket_reducer,
    _normalize_adf_body,
    _read_latest_status,
    _resolve_priority,
    _resolve_tracker_dir,
    _write_event_file,
    recover_status_label,
)

# Loop-breaker marker (mirrors inbound_differ.RECONCILER_MARKER). Comments whose
# body carries this marker are our own outbound echoes, so the comment-history
# bootstrap in _apply_inbound_create — can skip our own echoes.
_RECONCILER_MARKER_APPLIER = "<!-- rebar:reconciler-echo -->"


# ---------------------------------------------------------------------------
# _apply_inbound_create phase helpers
# ---------------------------------------------------------------------------


def _inbound_create_write_create_event(
    fields, payload, jira_key, local_id, repo_root
) -> tuple[Any, list, Any]:
    """Phase: resolve ticket_type + tags + parent, then write the CREATE event.

    Returns ``(tracker_dir, raw_labels, create_path)`` for the phases that follow
    (STATUS event / comment bootstrap consume tracker_dir + raw_labels).
    """
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
    # Prefer a pre-resolved local parent ID, then derive it from Jira's key;
    # use an empty parent when neither is available.
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
        # Convert raw ADF to reducer text so later diffs can converge.
        "description": _normalize_adf_body(fields.get("description")),
        "parent_id": resolved_parent_id,
        "tags": tags,
    }
    if "priority" in fields:
        create_data["priority"] = _resolve_priority(fields["priority"])
    if fields.get("assignee"):
        create_data["assignee"] = _extract_name(fields["assignee"])
        # 2f13 (additive): mint/reuse a ghost identity for the inbound assignee when it
        # carries an opaque accountId — best-effort, never fails the create.
        # Imported here rather than at module scope: the mint is a rebar-facade mutation
        # and lives in ``apply_inbound_records``, which imports THIS module at module
        # scope for its back-compat re-export. A call-time import keeps that direction
        # one-way, and resolves the name on each call exactly as the pre-split
        # same-module reference did.
        from rebar.reconciler.apply_inbound_records import _ensure_inbound_assignee_identity

        _ensure_inbound_assignee_identity(fields["assignee"], repo_root)
    # Validate and record the Jira creation channel on this direct CREATE path.
    from rebar.reducer._version import validate_creation_channel

    create_data["creation_channel"] = validate_creation_channel("jira")
    create_data.update(projects_store.resolve_inbound_bridge_fields(jira_key, repo_root))
    create_path = _write_event_file(tracker_dir, local_id, "CREATE", create_data)
    return tracker_dir, raw_labels, create_path


def _cascade_inbound_status_parents(
    tracker_dir, local_id, current_status: str, target_status: str
) -> list[str]:
    """Write eligible ancestor STATUS events before the inbound child's event.

    Reuse the interactive cascade's edge and parent decision, but write event
    files within the batch. Skip absent/ineligible parents, guard cycles, and
    propagate write failures. Return paths in topmost-first order.
    """
    from rebar._commands.transition import _CASCADING_EDGES, _resolve_parent_in_status

    eligible = _CASCADING_EDGES.get((current_status, target_status))
    if eligible is None:
        return []
    # On every cascading edge the eligible parent status equals the child's current
    # status, so each ancestor takes the same (eligible -> target) edge — walking the
    # lookup up the chain is exactly the interactive cascade's recursion.
    seen = {local_id}
    chain: list[str] = []
    ticket_id = local_id
    while True:
        parent_id = _resolve_parent_in_status(str(tracker_dir), ticket_id, eligible)
        if parent_id is None or parent_id in seen:
            break
        chain.append(parent_id)
        seen.add(parent_id)
        ticket_id = parent_id
    written: list[str] = []
    for parent_id in reversed(chain):  # topmost eligible ancestor first
        path = _write_event_file(
            tracker_dir,
            parent_id,
            "STATUS",
            {"status": target_status, "current_status": eligible},
        )
        written.append(str(path))
    return written


def _inbound_create_write_status_event(fields, raw_labels, tracker_dir, local_id) -> None:
    """Phase: write a STATUS event when the Jira status reverse-maps to non-default."""
    # A rebar-status label takes precedence over Jira workflow status;
    # write only statuses beyond the reducer's open default.
    local_status: str | None = recover_status_label(raw_labels)
    if local_status is None:
        jira_status = _extract_name(fields.get("status"))
        if jira_status:
            local_status = _jira_status_to_local(jira_status)
    if local_status and local_status != "open":
        # Move eligible parents before the new ticket's active STATUS event.
        _cascade_inbound_status_parents(tracker_dir, local_id, "open", local_status)
        _write_event_file(
            tracker_dir,
            local_id,
            "STATUS",
            {"status": local_status, "current_status": "open"},
        )


def _inbound_create_record_binding(mutation, binding_store, local_id, jira_key) -> None:
    """Bind the Jira issue and seed its adopt baseline."""
    # Bind immediately so the next outbound pass does not re-emit CREATE.
    if binding_store is not None:
        _bind_confirm = getattr(binding_store, "bind_confirm", None)
        if _bind_confirm is not None:
            _bind_confirm(local_id, jira_key)
            # ADOPT gate #4 (ADR 0027 §4c / ADR 0029 §3): seed the baseline from the
            # adopted Jira fields (payload["jira_fields"]) right after bind, so the
            # first outbound diff is empty (echo suppression). set_baseline filters
            # to the mirrored fields; it must run AFTER bind_confirm.
            _set_baseline = getattr(binding_store, "set_baseline", None)
            _adopted_fields = mutation.payload.get("jira_fields")
            if _set_baseline is not None and isinstance(_adopted_fields, dict):
                _set_baseline(local_id, _adopted_fields)
        else:
            import sys as _sys

            print(  # noqa: T201
                f"WARNING: inbound_create: binding store lacks bind_confirm; "
                f"{local_id!r}<->{jira_key!r} NOT bound at create — the next "
                f"pass will re-emit an outbound create (dedup-skip will "
                f"converge it).",
                file=_sys.stderr,
            )


def _inbound_create_writeback_jira(
    client: TicketTransport | None, jira_key, local_id, tracker_dir
) -> None:
    """Phase: write identity markers + bootstrap pre-existing comments back to Jira."""
    # Write rebar-id label + local_id entity property back to Jira so the
    # differ recognizes this issue as mirrored on subsequent passes (dedup).
    if client is not None:
        client.add_label(jira_key, f"rebar-id:{local_id}")
        client.set_entity_property(jira_key, "local_id", local_id)

    # Bootstrap Jira comments into local COMMENT events, skipping marked
    # outbound echoes and recording IDs for later dedup. Fetch errors warn
    # without failing CREATE.
    if client is not None:
        try:
            raw_comments = client.get_comments(jira_key)
            if not isinstance(raw_comments, list):
                raw_comments = []
        except Exception as exc:  # noqa: BLE001 — fail-open: skip comment bootstrap, CREATE still succeeds
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


# ---------------------------------------------------------------------------
# _apply_inbound_update phase helpers
# ---------------------------------------------------------------------------


def _inbound_update_write_edit_event(
    fields, tracker_dir, local_id, written, repo_root=None
) -> None:
    """Phase: map Jira→local scalar fields and write one EDIT event (if any)."""
    # Map field names to local reducer field names. The inbound differ
    # ALREADY maps Jira → local (see inbound_differ._map_jira_to_local_fields:
    # emits ``title`` / ``ticket_type`` / local-mapped ``status``). Accept
    # the local-keyed shape from the differ AND the legacy Jira-keyed
    # ``summary`` for back-compat with any caller that bypasses the differ.
    edit_fields: dict[str, Any] = {}
    # Keep the legacy summary fallback inline. Transform other scalar fields;
    # normalize raw ADF and turn an empty parent into a clear.
    if "title" in fields:
        edit_fields["title"] = fields["title"]
    elif "summary" in fields:
        edit_fields["title"] = fields["summary"]
    _scalar_transforms = {
        "description": lambda v: _normalize_adf_body(v) if isinstance(v, dict) else v,
        "priority": _resolve_priority,
        "assignee": _extract_name,
        "ticket_type": lambda v: v,
        "parent_id": lambda v: v or "",
    }
    for _fname, _transform in _scalar_transforms.items():
        if _fname in fields:
            edit_fields[_fname] = _transform(fields[_fname])

    # Mint a ghost identity from the canonical assignee object when available;
    # accept the raw Jira object from callers that bypass the differ.
    if "assignee" in fields:
        # Import at call time to keep the module dependency one-way.
        from rebar.reconciler.apply_inbound_records import _ensure_inbound_assignee_identity

        _ensure_inbound_assignee_identity(
            fields.get("assignee_identity") or fields["assignee"], repo_root
        )

    if edit_fields:
        path = _write_event_file(tracker_dir, local_id, "EDIT", {"fields": edit_fields})
        written.append(str(path))


def _inbound_update_write_status_event(fields, tracker_dir, local_id, written) -> None:
    """Write STATUS when Jira supplies a changed status."""
    if "status" in fields:
        raw_status = fields["status"]
        # The differ supplies local status strings; legacy callers supply
        # Jira dicts. Use shape to decide whether reverse mapping is needed.
        if isinstance(raw_status, dict):
            local_status = _jira_status_to_local(_extract_name(raw_status))
        else:
            local_status = raw_status
        # Read the prior status for the reducer's fork check.
        previous_status = _read_latest_status(tracker_dir, local_id)
        if local_status == previous_status:
            # An earlier cascade may have moved this ticket in the same batch.
            # Avoid a duplicate STATUS write.
            return
        # Move eligible ancestors before the child's STATUS event.
        written.extend(
            _cascade_inbound_status_parents(tracker_dir, local_id, previous_status, local_status)
        )
        path = _write_event_file(
            tracker_dir,
            local_id,
            "STATUS",
            {"status": local_status, "current_status": previous_status},
        )
        written.append(str(path))


def _inbound_update_apply_labels(mutation, payload, tracker_dir, local_id, written) -> None:
    """Apply inbound label changes through TAG_DELTA."""
    # Apply inbound add/remove labels as TAG_DELTA. Read current tags to
    # suppress no-ops; a whole-field EDIT would replace concurrent local tags.
    inbound_labels = payload.get("labels") or []
    if isinstance(inbound_labels, list) and inbound_labels:
        # Read current tags before filtering add/remove no-ops. On reducer
        # failure, warn and retry next pass instead of assuming an empty set.
        reducer_failed = False
        current_state: dict | None = None
        try:
            reducer_mod = _load_ticket_reducer()
            current_state = reducer_mod.reduce_ticket(str(tracker_dir / local_id))
        except Exception as _reducer_exc:  # noqa: BLE001 — see bc8f docstring
            reducer_failed = True
            print(  # noqa: T201
                f"[applier] WARN bc8f-guard: reducer raised while reading "
                f"current tags for {local_id} (target={mutation.target}); "
                f"skipping labels EDIT to avoid wiping local tags. "
                f"exc={type(_reducer_exc).__name__}: {_reducer_exc}",
                file=sys.stderr,
            )
        if not reducer_failed and current_state is None:
            print(  # noqa: T201
                f"[applier] WARN bc8f-guard: reducer returned None for "
                f"{local_id} (ticket dir not yet materialized?); skipping "
                f"labels EDIT to avoid wiping local tags. The next "
                f"reconciler pass will retry.",
                file=sys.stderr,
            )

        if current_state is not None:
            # P2.3: emit a convergent TAG_DELTA (add/remove) instead of a
            # whole-field EDIT.tags — the last live whole-field tag writer is gone,
            # so a Jira-side label change no longer clobbers a concurrent local add.
            # We still read current tags for NO-OP SUPPRESSION (only add absent,
            # only remove present); the delta itself can never wipe other tags.
            from rebar.reducer._version import TAG_DELTA

            current_tags: list[str] = list(current_state.get("tags", []) or [])
            added: list[str] = []
            removed: list[str] = []
            for entry in inbound_labels:
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action")
                label_name = entry.get("label", "")
                if not label_name or not isinstance(label_name, str):
                    continue
                if action == "add" and label_name not in current_tags and label_name not in added:
                    added.append(label_name)
                elif (
                    action == "remove" and label_name in current_tags and label_name not in removed
                ):
                    removed.append(label_name)
            # Dedup added∩removed (a contradictory inbound pass): add-wins, matching
            # the reducer's intra-event contract — never emit a label in both lists.
            removed = [t for t in removed if t not in added]
            if added or removed:
                # Mark inbound origin so Jira-added tags are not treated as
                # local user intent on a later remove.
                path = _write_event_file(
                    tracker_dir,
                    local_id,
                    TAG_DELTA,
                    {"added": added, "removed": removed, "source": "inbound"},
                )
                written.append(str(path))


def _inbound_update_apply_comments(payload, tracker_dir, local_id, written) -> None:
    """Write COMMENT events for new Jira comments."""
    # Store new Jira comments as text with IDs for outbound echo suppression.
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
