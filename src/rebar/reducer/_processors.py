"""Event-type processors for the ticket reducer.

Each function takes the current mutable state dict, the parsed event dict,
and any ancillary data needed (e.g. filepath for conflict recording), and
applies the event's effect to state in-place.  All processors return None.
"""

from __future__ import annotations

import json
import os
import sys

from ._version import KNOWN_EVENT_TYPES, TAG_DELTA


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
    from ._state import make_error_dict

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
        from rebar._alias import compute_alias

        state["alias"] = compute_alias(ticket_id)
    state["description"] = data.get("description") or ""
    state["tags"] = data.get("tags", [])
    # Provenance (P1.2 import): a ticket re-created by `rebar import` carries the
    # ORIGINAL store's id/date/author/env as source_* on the CREATE data (fresh
    # local id + fresh HLC timestamp are used for the new event; foreign HLC
    # timestamps are never injected). Surface them additively — present only when
    # the CREATE carried them, so a normally-created ticket's state is byte-for-byte
    # unchanged. Mirrors the conditional jira_comment_id handling in process_comment.
    for _src_key in ("source_id", "source_created_at", "source_author", "source_env"):
        _src_val = data.get(_src_key)
        if _src_val is not None:
            state[_src_key] = _src_val
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
        # value — bug e60b-e698, regression test:
        # test_reducer_applies_multiple_status_events_current_status_mismatch_resolves_fork).
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
        # Advance to THIS event's OWN UUID (not its data parent-pointer) so a
        # subsequent concurrent sibling forks against this event's identity and
        # resolves by the lexical-UUID rule above — deterministically and
        # independent of replay order, exactly as this docstring / CLAUDE.md
        # describe. Bug 8874: the previous `data["parent_status_uuid"]` stored the
        # common-parent pointer, so two siblings from an EMPTY parent compared the
        # incoming uuid against "" and the later-replayed event won by insertion
        # order rather than by UUID. Matches the fork branch above, which already
        # records the winner's own UUID.
        state["parent_status_uuid"] = event.get("uuid") or ""
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
    # Provenance (P1.2 import): an imported comment carries the original comment's
    # author/timestamp as source_* (the new COMMENT event records the importer as
    # author + a fresh HLC timestamp). Surfaced only when present, so non-imported
    # comments keep their existing two-field shape.
    for _src_key in ("source_author", "source_created_at"):
        _src_val = data.get(_src_key)
        if _src_val is not None:
            _entry[_src_key] = _src_val
    state["comments"].append(_entry)


def process_link(state: dict, event: dict, data: dict, tracker_dir: str | None = None) -> None:
    """Apply a LINK event: append a dep entry to state.deps.

    When tracker_dir is provided, attempt to resolve an alias-form or short-hex
    target_id to its canonical UUID via resolve_ticket_id.  On failure (alias
    unresolvable, resolver unavailable, or no tracker_dir) the verbatim value
    is stored as a graceful fallback so no data is lost.

    Deliberate boundary (do NOT "fix" this as a wrong-answer — story lean-sloth-ham
    investigated it): when a ``depends_on`` target cannot be resolved because its
    directory is absent, the readiness paths treat that blocker as **closed** — this
    is the intentional *tombstone-awareness* invariant (an archived/deleted blocker
    must not block its dependents forever), see ``_status._get_ticket_status`` and
    ``tests/scripts/graph/test_graph_unresolved_blocker.py``. A missing-target
    ``depends_on`` is therefore correctly NOT a blocker. Failing closed on every
    unresolved target was tried and rejected: ``resolve_ticket_id`` returns ``None``
    for BOTH normal archival and a genuinely-bogus alias, indistinguishably, so a
    blanket fail-closed would break archived-blocker unblocking (a tested invariant).
    """
    raw_target = data.get("target_id", data.get("target", ""))
    resolved_target = raw_target
    if tracker_dir and raw_target:
        try:
            # local import avoids a module-load circular dep with the resolver
            from rebar._engine_support.resolver import resolve_ticket_id

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
    state["deps"] = [d for d in state["deps"] if d.get("link_uuid") != link_uuid_to_remove]


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
    open. A ticket that was DELETED (delete writes STATUS(deleted)+ARCHIVED) is
    left FULLY untouched: it keeps status="deleted" AND archived=True, so it
    stays hidden. (Default `list` excludes archived but not deleted, so clearing
    archived on a deleted ticket would resurrect it into the listing — review
    H1.) The status!="deleted" guard on the whole block enforces this.
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
    if (
        data.get("target_event_type") == "ARCHIVED"
        and state.get("archived")
        and state.get("status") != "deleted"
    ):
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


def process_signature(state: dict, event: dict, data: dict) -> None:
    """Apply a SIGNATURE event: replace state.signature (last-writer-wins).

    Stores the latest cryptographic attestation — the manifest of verified steps,
    the HMAC signature, the environment key fingerprint, and audit metadata — so
    ``verify-signature`` can recompute and certify it. Mirrors the FILE_IMPACT /
    VERIFY_COMMANDS last-writer-wins shape, so the record survives compaction (the
    compactor builds the SNAPSHOT compiled_state via this reducer). The signed-at
    timestamp falls back to the event timestamp for forward-compat records.
    """
    _manifest = data.get("manifest")
    state["signature"] = {
        # Coerce to a list: never persist a non-list truthy value (e.g. a dict)
        # into reduced state, which would leak a malformed shape into show/MCP
        # output (security-adjacent state — fail closed, like verify_record).
        "manifest": _manifest if isinstance(_manifest, list) else [],
        "algorithm": data.get("algorithm"),
        "signature": data.get("signature"),
        "key_id": data.get("key_id"),
        "head_sha": data.get("head_sha"),
        "signed_at": data.get("signed_at") or event.get("timestamp"),
        "author": event.get("author"),
    }


def process_workflow_run(state: dict, event: dict, data: dict) -> None:
    """Apply a WORKFLOW_RUN event: per-key LWW into ``state.workflow_runs[run_id]``.

    Workflow run-state lives on rebar's only durable surface — the target ticket's
    append-only event log (epic a88f / WS-C). Each event carries the COMPLETE
    current run record (status, timing, inputs, the captured now/uuid for
    deterministic replay, …); replay keeps the LAST event per ``run_id`` because
    event files sort by ``{HLC-timestamp}-{uuid}`` and that order is identical on
    every clone, so concurrent runs converge deterministically with no extra
    tie-break. The map is created lazily (``setdefault``) so a ticket that never ran
    a workflow keeps its exact prior shape — no empty ``workflow_runs`` key leaks
    into ``show``/``list`` for the common case. Only the one ``run_id`` key is
    replaced, never the whole map (so two runs on one ticket don't clobber).
    """
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return
    runs = state.setdefault("workflow_runs", {})
    runs[run_id] = dict(data)


def process_workflow_step(state: dict, event: dict, data: dict) -> None:
    """Apply a WORKFLOW_STEP event: per-key LWW into
    ``state.workflow_steps[run_id][frame_key]``.

    The step's idempotency marker + result: the executor commits one of these AFTER
    a step's effect (WS-C3), carrying the full per-step record (status, outputs,
    error, captured non-determinism). The slot key is the **frame key** — the bare
    ``step_id`` at the top frame, or an iteration-embedding path (e.g.
    ``L#2/attempt``) for a step inside a loop/map body (v2). So a step that runs once
    per iteration gets a DISTINCT marker per iteration — the (run_id, step_id,
    iteration) keying the v2 interpreter relies on for exactly-once replay — while a
    flat (v1 / migrated) run stays keyed exactly as before (frame_key == step_id;
    older events with no ``frame_key`` fall back to ``step_id``). Per key it is
    last-writer-wins in replay (HLC+UUID filename order), so a re-run/retry's later
    event supersedes the earlier one and all clones agree. Lazy + per-key like
    :func:`process_workflow_run` (no nested per-iteration dict, so the flat hot path
    is untouched).
    """
    run_id = data.get("run_id")
    step_id = data.get("step_id")
    if not (isinstance(run_id, str) and run_id and isinstance(step_id, str) and step_id):
        return
    frame_key = data.get("frame_key")
    key = frame_key if isinstance(frame_key, str) and frame_key else step_id
    steps = state.setdefault("workflow_steps", {})
    run_steps = steps.setdefault(run_id, {})
    run_steps[key] = dict(data)


def process_commits(state: dict, event: dict, data: dict) -> None:
    """Apply a COMMITS event: union commit records into ``state.commits`` (WS-H).

    The code-review example workflow needs commit SHAs attached to a ticket as
    input. Each event carries ``data.commits`` — a list of SHAs (strings) or commit
    records ({sha, message?, author?, …}); they are UNIONED into the ticket's
    ``commits`` list, deduplicated by ``sha`` (first occurrence in replay order
    wins). Union-add is order-insensitive for the SET, and replay order is the
    deterministic HLC+UUID filename order, so every clone converges to the same
    list. Lazy/additive (``setdefault``-free guard) so a ticket with no commits
    keeps its exact prior shape; restored verbatim by SNAPSHOT, so it survives
    compaction. Never surfaced to Jira (the outbound differ is field-driven and
    does not read ``commits``)."""
    incoming = data.get("commits")
    if not isinstance(incoming, list) or not incoming:
        return
    existing = state.get("commits") or []
    seen = {c.get("sha") for c in existing if isinstance(c, dict) and c.get("sha")}
    merged = list(existing)
    for item in incoming:
        record = {"sha": item} if isinstance(item, str) else item
        if not isinstance(record, dict):
            continue
        sha = record.get("sha")
        if not sha or sha in seen:
            continue
        seen.add(sha)
        merged.append(record)
    if merged:
        state["commits"] = merged


def process_tag_delta(state: dict, data: dict) -> None:
    """Apply a TAG_DELTA event: add/remove tag deltas into ``state.tags`` (P2.3).

    Replaces the whole-field ``EDIT.tags`` last-writer-wins clobber: each event
    carries ``data.added`` / ``data.removed`` (lists of tag strings). We mutate the
    CURRENT ``state.tags`` in replay order — remove the ``removed`` set, then union
    the ``added`` set — so two clones concurrently adding different tags both
    survive (union is order-insensitive) and replay order is the deterministic
    HLC+UUID filename order, so every clone converges to the same list.

    Idempotent on replay (skip-if-present add / skip-if-absent remove). INTRA-EVENT
    CONFLICT CONTRACT: if a tag is in both ``added`` and ``removed``, **add wins**
    (remove runs first, then add) — enforced and tested here at the reducer, the
    cross-version convergence point, never delegated to the command layer (a
    future/buggy/old client may emit a contradictory pair). Defensive ``isinstance``
    guards mirror :func:`process_commits` / :func:`process_workflow_run`: a non-list
    ``added``/``removed`` is treated as empty. Legacy/historical ``EDIT.tags`` is
    still handled by :func:`process_edit` (unchanged) and forms the replay base.
    """
    added = data.get("added")
    removed = data.get("removed")
    if not isinstance(added, list):
        added = []
    if not isinstance(removed, list):
        removed = []
    tags = list(state.get("tags") or [])
    remove_set = {t for t in removed if isinstance(t, str)}
    if remove_set:
        tags = [t for t in tags if t not in remove_set]
    for t in added:
        if isinstance(t, str) and t and t not in tags:
            tags.append(t)
    state["tags"] = tags


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
            snapshot_source_uuids = set(event.get("data", {}).get("source_event_uuids", []))

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
    # Running max of event timestamps over the events that actually shape state
    # (post-snapshot/applied events + the SNAPSHOT's own compacted_at). Surfaced
    # as the derived ``updated_at`` (P1.1). Pre-snapshot events are skipped here
    # but their effect is folded into the SNAPSHOT's compacted_at, so the max
    # stays correct across compaction. Kept off the event log: compact.py strips
    # ``updated_at`` before serializing compiled_state (byte-parity guard).
    max_ts: int | None = None

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

        # Fold this applied event into the updated_at running max. A SNAPSHOT
        # re-seeds from its compacted_at (replay discards the pre-snapshot events
        # it summarizes), so a freshly-compacted, untouched ticket reports
        # updated_at == compacted_at.
        ev_ts = data.get("compacted_at") if event_type == "SNAPSHOT" else event.get("timestamp")
        if ev_ts is not None and (max_ts is None or ev_ts > max_ts):
            max_ts = ev_ts

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
        elif event_type == "SIGNATURE":
            process_signature(state, event, data)
        elif event_type == "WORKFLOW_RUN":
            process_workflow_run(state, event, data)
        elif event_type == "WORKFLOW_STEP":
            process_workflow_step(state, event, data)
        elif event_type == "COMMITS":
            process_commits(state, event, data)
        elif event_type == TAG_DELTA:
            process_tag_delta(state, data)
        elif event_type == "ARCHIVED":
            process_archived(state)
        elif event_type == "SNAPSHOT":
            process_snapshot(state, data)
        elif event_type not in KNOWN_EVENT_TYPES:
            # Forward compatibility (schema-version rule, see _version.py): an event
            # kind a NEWER rebar introduced. Preserved-and-ignored — skipped here
            # without error so the ticket stays readable; ticket-compact.sh keeps the
            # file so an older clone's compaction doesn't destroy a newer clone's data.
            pass

    # Derived presentation field (not an event-log field). None when no applied
    # event carried a timestamp; the None-last sort key handles that.
    state["updated_at"] = max_ts
    return valid_event_count, None
