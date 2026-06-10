"""Event-type processors for the ticket reducer.

Each function takes the current mutable state dict, the parsed event dict,
and any ancillary data needed (e.g. filepath for conflict recording), and
applies the event's effect to state in-place.  All processors return None.
"""

from __future__ import annotations

import json
import os
import sys


def process_create(
    state: dict,
    event: dict,
    data: dict,
    ticket_id: str,
    cache_path: str,
    dir_hash: str,
) -> dict | None:
    """Apply a CREATE event to state.

    Returns a fsck_needed error-state dict if required fields are missing,
    otherwise mutates state in-place and returns None.
    """
    from ticket_reducer._state import make_error_dict

    if not data.get("ticket_type") or not data.get("title"):
        fsck_result = make_error_dict(ticket_id, "fsck_needed", "corrupt_create_event")
        # Write the fsck result to cache immediately so callers get consistent results
        try:
            cache_tmp = cache_path + ".tmp"
            with open(cache_tmp, "w", encoding="utf-8") as tf:
                json.dump(
                    {"dir_hash": dir_hash, "state": fsck_result},
                    tf,
                    ensure_ascii=False,
                )
            os.rename(cache_tmp, cache_path)
        except OSError:
            pass
        return fsck_result

    state["ticket_id"] = ticket_id
    state["ticket_type"] = data.get("ticket_type")
    state["title"] = data.get("title")
    state["author"] = event.get("author")
    state["created_at"] = event.get("timestamp")
    state["env_id"] = event.get("env_id")
    state["parent_id"] = data.get("parent_id") or None
    state["priority"] = data.get("priority")
    state["assignee"] = data.get("assignee")
    # Adjective-noun-noun alias (3363-fa8b): ticket_create computes this and
    # writes it onto the CREATE event's data; the reducer must propagate it
    # into compiled state so resolve_ticket_id and ticket_show can return
    # human-friendly aliases. Without this assignment, alias is silently
    # dropped between persistence and compiled state, defeating the entire
    # alias system. For tickets created before the alias feature shipped
    # (data.alias is missing), backfill at read time using the deterministic
    # ticket_id-derived alias so legacy tickets surface the same alias they
    # would have been assigned at creation.
    stored_alias = data.get("alias")
    if stored_alias:
        state["alias"] = stored_alias
    else:
        from ticket_reducer._alias import compute_alias

        state["alias"] = compute_alias(ticket_id)
    state["description"] = data.get("description") or ""
    state["tags"] = data.get("tags", [])
    return None


def process_status(state: dict, event: dict, data: dict, filepath: str) -> None:
    """Apply a STATUS event with fork detection and lexical UUID tie-break.

    If current_status in the event doesn't match state['status'], a fork has
    been detected (two competing chains diverged). Resolve by comparing the
    incoming event's own UUID (``event.get("uuid")``) against the UUID already
    recorded in ``state['parent_status_uuid']`` — the lexically lower UUID
    wins and its target_status is applied. Using event-own UUIDs (not parent
    pointers) makes concurrent siblings with the same parent resolve
    deterministically regardless of replay order (bug 1587-4816).

    On a normal (non-fork) update, ``state['status']`` is updated to the
    event's target status and ``state['parent_status_uuid']`` is advanced to
    the event's own UUID so subsequent forks compare against the winner's
    identity, not its parent.

    The legacy ``state['conflicts']`` key is never written and is removed if
    found (e.g. replayed from an old SNAPSHOT compiled_state).
    """
    # Remove legacy conflicts key unconditionally — new behavior never uses it.
    state.pop("conflicts", None)

    current_status = data.get("current_status")
    if current_status is not None and current_status != state["status"]:
        # Fork detected: two chains have diverged.
        #
        # Tie-break uses the events' own UUIDs (not parent pointers) so that
        # concurrent siblings — two STATUS events with the same parent pointer —
        # resolve deterministically regardless of replay order (bug 1587-4816).
        # state["parent_status_uuid"] is advanced to the WINNING event's own UUID
        # so subsequent forks compare against the winner's identity, not its parent.
        incoming_uuid = event.get("uuid") or ""
        existing_uuid = state.get("parent_status_uuid") or ""

        # Lower lexical UUID wins. Empty existing_uuid means no prior fork
        # winner has been recorded, so the incoming event wins unconditionally
        # (otherwise any non-empty incoming UUID > "" and the existing-empty
        # branch would always win, leaving state.status stuck at the loser's
        # value — bug e60b-e698, test_reducer_applies_multiple_status_events_current_status_mismatch_resolves_fork).
        if not existing_uuid or incoming_uuid <= existing_uuid:
            # Incoming event wins.
            winner_uuid = incoming_uuid
            loser_uuid = existing_uuid
            # Use last_status_env_id (set by most recent STATUS event) so we log
            # the losing STATUS author's env, not the ticket creator's env.
            loser_env_id = state.get("last_status_env_id") or ""
            state["status"] = data.get("status", state["status"])
            state["parent_status_uuid"] = incoming_uuid  # winner's own UUID
        else:
            # Existing chain wins; keep state as-is.
            winner_uuid = existing_uuid
            loser_uuid = incoming_uuid
            loser_env_id = event.get("env_id", "") or ""

        ticket_id = state.get("ticket_id", "")
        print(
            f"PARENT_CHAIN_FORK_RESOLVED ticket={ticket_id}"
            f" winner={winner_uuid}"
            f" dropped=[{loser_uuid}]"
            f" loser_env_id=[{loser_env_id}]",
            file=sys.stderr,
        )
    else:
        state["status"] = data.get("status", state["status"])
        state["parent_status_uuid"] = data.get(
            "parent_status_uuid", state.get("parent_status_uuid", "")
        )
        state["last_status_env_id"] = event.get("env_id") or ""


def process_comment(state: dict, event: dict, data: dict) -> None:
    """Apply a COMMENT event: append normalized body to state.comments.

    Coerces non-string bodies (e.g. Jira ADF dicts) to JSON string so
    downstream string-parsing consumers never receive a dict (b108-f088).
    Uses explicit None check — truthiness check treats {} as falsy (6bc8-91bc).
    """
    _raw_body = data.get("body")
    if _raw_body is None:
        _raw_body = ""
    elif not isinstance(_raw_body, str):
        _raw_body = json.dumps(_raw_body)
    # Bug 85a1 (Gap 1): preserve the source jira_comment_id so the outbound
    # differ's loop-breaker can skip comments that originated from Jira-side
    # inbound pulls. Without this the reconciler would re-push every
    # inbound-pulled comment back to Jira on the next outbound pass.
    _entry: dict = {
        "body": _raw_body,
        "author": event.get("author"),
        "timestamp": event.get("timestamp"),
    }
    _jira_comment_id = data.get("jira_comment_id")
    if _jira_comment_id is not None:
        _entry["jira_comment_id"] = str(_jira_comment_id)
    state["comments"].append(_entry)


def process_link(
    state: dict, event: dict, data: dict, tracker_dir: str | None = None
) -> None:
    """Apply a LINK event: append a dep entry to state.deps.

    When tracker_dir is provided, attempt to resolve an alias-form or short-hex
    target_id to its canonical UUID via resolve_ticket_id.  On failure (alias
    unresolvable, resolver unavailable, or no tracker_dir) the verbatim value
    is stored as a graceful fallback so no data is lost.
    """
    raw_target = data.get("target_id", data.get("target", ""))
    resolved_target = raw_target
    if tracker_dir and raw_target:
        try:
            from ticket_resolver import resolve_ticket_id  # local import avoids circular dep

            canonical = resolve_ticket_id(raw_target, tracker_dir)
            if canonical:
                resolved_target = canonical
        except Exception:  # noqa: BLE001 — resolver is best-effort; never crash the reducer
            pass
    state["deps"].append(
        {
            "target_id": resolved_target,
            "relation": data.get("relation", ""),
            "link_uuid": event["uuid"],
        }
    )


def process_unlink(state: dict, data: dict) -> None:
    """Apply an UNLINK event: remove the dep entry matching link_uuid (noop if unknown)."""
    link_uuid_to_remove = data.get("link_uuid")
    state["deps"] = [
        d for d in state["deps"] if d.get("link_uuid") != link_uuid_to_remove
    ]


def process_bridge_alert(state: dict, event: dict, data: dict, event_uuid: str) -> None:
    """Apply a BRIDGE_ALERT event: add or resolve an alert in state.bridge_alerts.

    Reason normalization: prefer data.alert_type (inbound), fall back to
    data.reason (outbound), then data.detail, then empty string.
    Resolution: resolves_uuid (test contract) takes precedence over alert_uuid (spec).
    """
    reason = data.get("alert_type") or data.get("reason") or data.get("detail") or ""
    if data.get("resolved"):
        target_uuid = data.get("resolves_uuid") or data.get("alert_uuid")
        matched = False
        for existing in state["bridge_alerts"]:
            if existing.get("uuid") == target_uuid:
                existing["resolved"] = True
                matched = True
        if not matched:
            state["bridge_alerts"].append(
                {
                    "uuid": event_uuid,
                    "reason": reason,
                    "timestamp": event.get("timestamp"),
                    "resolved": True,
                }
            )
    else:
        state["bridge_alerts"].append(
            {
                "uuid": event_uuid,
                "reason": reason,
                "timestamp": event.get("timestamp"),
                "resolved": False,
            }
        )


def process_revert(state: dict, event: dict, data: dict, event_uuid: str) -> None:
    """Apply a REVERT event: append a revert record to state.reverts.

    Reverting an ARCHIVED event also UN-ARCHIVES the projection (bug
    vocal-jig-apron). The designed unarchive seam (``rebar revert <id>
    <archived-uuid>``) removes the .archived marker and is recognised by the
    list fast-path, but replay still re-applied the ARCHIVED event, leaving
    compiled state at status=archived/archived=True (ticket stayed hidden).
    Clear the archived projection so the ticket is visible again with status
    open. A ticket that was DELETED (delete writes STATUS(deleted)+ARCHIVED)
    keeps its terminal deleted status: process_archived never set
    status=archived for it, so the ``== "archived"`` guard leaves it untouched.
    """
    state["reverts"].append(
        {
            "uuid": event_uuid,
            "target_event_uuid": data.get("target_event_uuid"),
            "target_event_type": data.get("target_event_type"),
            "reason": data.get("reason", ""),
            "timestamp": event.get("timestamp"),
            "author": event.get("author"),
        }
    )
    if data.get("target_event_type") == "ARCHIVED" and state.get("archived"):
        state["archived"] = False
        if state.get("status") == "archived":
            state["status"] = "open"


def process_edit(state: dict, data: dict) -> None:
    """Apply an EDIT event: merge data.fields into state (last-writer-wins).

    Tags stored as comma-separated string in event; convert to list.
    If the value is already a list (e.g. from a SNAPSHOT), keep it.
    Unknown field names (not present in state) are silently ignored.
    """
    fields = data.get("fields", {})
    for field_name, new_value in fields.items():
        if field_name not in state:
            continue
        if field_name == "tags":
            if isinstance(new_value, list):
                state["tags"] = new_value
            elif isinstance(new_value, str):
                state["tags"] = [t.strip() for t in new_value.split(",") if t.strip()]
            else:
                state["tags"] = []
        else:
            state[field_name] = new_value


def process_file_impact(state: dict, event: dict, data: dict) -> None:
    """Apply a FILE_IMPACT event: replace state.file_impact (last-writer-wins).

    Uses `or []` to handle both missing key and JSON null values.
    """
    state["file_impact"] = data.get("file_impact") or []


def process_verify_commands(state: dict, event: dict, data: dict) -> None:
    """Apply a VERIFY_COMMANDS event: replace state.verify_commands (last-writer-wins).

    Mirrors the jq `show` reducer semantics (`.verify_commands = ev.data.verify_commands // []`)
    and the FILE_IMPACT processor. Without this, `verify_commands` was produced only
    by the jq reducer (show/get-file-impact), never by the Python reducer — so it was
    invisible in list/search and silently DROPPED when a ticket was compacted into a
    SNAPSHOT (the compactor builds compiled_state via this reducer). `or []` handles
    both missing key and JSON null.
    """
    state["verify_commands"] = data.get("verify_commands") or []


def process_archived(state: dict) -> None:
    """Apply an ARCHIVED event: mark ticket archived and reflect in status field.

    Preserves a prior 'deleted' status (delete writes STATUS(deleted) + ARCHIVED;
    the deleted terminal state must win over the archived projection).
    """
    state["archived"] = True
    if state.get("status") != "deleted":
        state["status"] = "archived"


def process_snapshot(state: dict, data: dict) -> None:
    """Apply a SNAPSHOT event: restore all fields from compiled_state."""
    compiled_state = data.get("compiled_state", {})
    for key, value in compiled_state.items():
        state[key] = value


def scan_for_latest_snapshot(
    event_files: list[str],
) -> tuple[int | None, set[str]]:
    """Pass 1: scan all events to find the latest SNAPSHOT index and its source UUIDs.

    Returns (latest_snapshot_idx, snapshot_source_uuids).
    latest_snapshot_idx is None if no SNAPSHOT was found.
    """
    latest_snapshot_idx: int | None = None
    snapshot_source_uuids: set[str] = set()

    for idx, filepath in enumerate(event_files):
        try:
            with open(filepath, encoding="utf-8") as f:
                event = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if event.get("event_type") == "SNAPSHOT":
            latest_snapshot_idx = idx
            snapshot_source_uuids = set(
                event.get("data", {}).get("source_event_uuids", [])
            )

    return latest_snapshot_idx, snapshot_source_uuids


def replay_events(
    state: dict,
    event_files: list[str],
    ticket_id: str,
    cache_path: str,
    dir_hash: str,
    tracker_dir: str | None = None,
) -> tuple[int, dict | None]:
    """Pass 2: replay events onto state, applying each processor in order.

    Skips events before the latest SNAPSHOT index and events whose UUID appears
    in snapshot_source_uuids (already captured in the SNAPSHOT compiled_state).

    Returns (valid_event_count, early_return_result).
    early_return_result is non-None only when a corrupt CREATE is encountered
    (returns the fsck_needed error dict immediately).
    """
    latest_snapshot_idx, snapshot_source_uuids = scan_for_latest_snapshot(event_files)
    start_idx = latest_snapshot_idx if latest_snapshot_idx is not None else 0
    valid_event_count = 0
    seen_uuids: set[str] = set()

    for idx, filepath in enumerate(event_files):
        try:
            with open(filepath, encoding="utf-8") as f:
                event = json.load(f)
        except (json.JSONDecodeError, OSError):
            print(f"WARNING: skipping corrupt event {filepath}", file=sys.stderr)
            continue

        valid_event_count += 1

        if idx < start_idx:
            continue

        event_uuid = event.get("uuid", "")
        if event_uuid and event_uuid in snapshot_source_uuids:
            continue

        # Dedup by UUID among events that actually replay: first occurrence in
        # filename order wins; a duplicate event file (same payload uuid under a
        # different filename) is skipped so it applies exactly once. Scoped to
        # post-snapshot, non-snapshot-source events, so zero interaction with
        # the compaction/snapshot machinery (bug 944c-374d).
        if event_uuid:
            if event_uuid in seen_uuids:
                continue
            seen_uuids.add(event_uuid)

        event_type = event.get("event_type", "")
        data = event.get("data", {})

        if event_type == "CREATE":
            result = process_create(state, event, data, ticket_id, cache_path, dir_hash)
            if result is not None:
                return valid_event_count, result
        elif event_type == "STATUS":
            process_status(state, event, data, filepath)
        elif event_type == "COMMENT":
            process_comment(state, event, data)
        elif event_type == "LINK":
            process_link(state, event, data, tracker_dir=tracker_dir)
        elif event_type == "UNLINK":
            process_unlink(state, data)
        elif event_type == "BRIDGE_ALERT":
            process_bridge_alert(state, event, data, event_uuid)
        elif event_type == "REVERT":
            process_revert(state, event, data, event_uuid)
        elif event_type == "EDIT":
            process_edit(state, data)
        elif event_type == "FILE_IMPACT":
            process_file_impact(state, event, data)
        elif event_type == "VERIFY_COMMANDS":
            process_verify_commands(state, event, data)
        elif event_type == "ARCHIVED":
            process_archived(state)
        elif event_type == "SNAPSHOT":
            process_snapshot(state, data)
        # Unknown event types are silently ignored

    return valid_event_count, None
