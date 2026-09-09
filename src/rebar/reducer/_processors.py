"""Event-type processors for the ticket reducer.

Each function takes the current mutable state dict, the parsed event dict,
and any ancillary data needed (e.g. filepath for conflict recording), and
applies the event's effect to state in-place.  All processors return None.
"""

from __future__ import annotations

import json
import logging
import os
from typing import get_args

from rebar.types import TicketStatus

from ._managed_refs import add_managed_ref, seed_managed_refs_from_current
from ._processors_identity import (
    _bootstrap_genesis_keyring,
    _fold_author_attribution,
    _rederive_keyring_keys,  # noqa: F401  (re-exported for back-compat)
    attestation_kind,
    process_key_event,  # noqa: F401  (re-exported for back-compat)
    process_signature,  # noqa: F401  (re-exported for back-compat)
)
from ._processors_status import (
    _fold_claimed_session,  # noqa: F401  (re-exported for back-compat)
    _fold_close_metadata,  # noqa: F401  (re-exported for back-compat)
    _fold_plan_review_phase,  # noqa: F401  (re-exported for back-compat)
    process_status,  # noqa: F401  (re-exported for back-compat)
)
from ._version import LEGACY_JIRA_AUTHOR, LEGACY_JIRA_ENV_ID

logger = logging.getLogger(__name__)

#: Derived from the canonical ``TicketStatus`` rather than re-listed (mirror F6).
#:
#: This is not a description but an ASSERTION: the snapshot reader below raises
#: ``ValueError("unknown ticket status in snapshot")`` on anything outside it. A hand-copy
#: that lagged ``TicketStatus`` would therefore make a newer clone's snapshot UNREADABLE —
#: it raises rather than degrading, unlike the event-type path which preserves-and-ignores.
_KNOWN_TICKET_STATUSES = frozenset(get_args(TicketStatus))


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
        from ._cache import write_cache

        fsck_result = make_error_dict(ticket_id, "fsck_needed", "corrupt_create_event")
        # Write the fsck result to cache immediately so callers get consistent results
        write_cache(cache_path, dir_hash, fsck_result, os.path.dirname(cache_path))
        return fsck_result

    state["ticket_id"] = ticket_id
    state["ticket_type"] = data.get("ticket_type")
    state["title"] = data.get("title")
    # Genesis status (soup-drift-augur): a CREATE event MAY carry a `status`. The
    # `rebar idea` command is its sole producer of a non-`open` genesis (`status=idea`)
    # so an idea is born in `idea` — never momentarily `open`/claimable. Default to the
    # value make_initial_state already seeded (`open`) when absent, so a normal CREATE
    # is byte-for-byte unchanged.
    state["status"] = data.get("status", state["status"])
    state["author"] = event.get("author")
    # Denormalized author attribution (epic gnu-whale-ichor): surface top-level
    # author_email (always, for a post-change CREATE) + author_id (when resolved),
    # present-only so a pre-change CREATE reduces byte-identically.
    _fold_author_attribution(state, event)
    state["created_at"] = event.get("timestamp")
    state["env_id"] = event.get("env_id")
    state["parent_id"] = data.get("parent_id") or None
    # Managed-ref provenance (safe-luge-nog): a parent set at creation is a
    # reference we manage from birth — fold it so a later detach can propagate.
    add_managed_ref(state, "parent", state["parent_id"])
    state["priority"] = data.get("priority")
    state["assignee"] = data.get("assignee")
    # Project the stored alias for lookup and display. Events without one derive the same
    # deterministic alias from ``ticket_id``.
    stored_alias = data.get("alias")
    if stored_alias:
        state["alias"] = stored_alias
    else:
        from rebar._alias import compute_alias

        state["alias"] = compute_alias(ticket_id)
    state["description"] = data.get("description") or ""
    state["tags"] = data.get("tags", [])
    # Bridge/project fields (story cef7). `bridge_project` is projected PRESENT-ONLY via a
    # key-presence check (NOT truthiness) so the three states stay distinguishable after
    # replay: absent key leaves the seeded None, an explicit "" projects "" (never-sync),
    # and a non-empty key projects the sync target. `repos` defaults to the seeded [].
    if "bridge_project" in data:
        state["bridge_project"] = data["bridge_project"]
    state["repos"] = data.get("repos", state.get("repos", []))
    # Project imported source metadata only when present. The local event retains its own ID and
    # HLC, while ordinary CREATE events keep their existing shape.
    for _src_key in ("source_id", "source_created_at", "source_author", "source_env"):
        _src_val = data.get(_src_key)
        if _src_val is not None:
            state[_src_key] = _src_val
    # Detection-channel capture (ticket d3ed): present-only, mirrors source_* above.
    _detected_by_val = data.get("detected_by")
    if _detected_by_val is not None:
        state["detected_by"] = _detected_by_val
    # Project immutable ingress provenance. Recorded values identify CLI, MCP, Python, Jira, or
    # import creation. Missing values stay ``unknown`` until legacy inference runs.
    state["creation_channel"] = data.get("creation_channel", "unknown")
    # Legacy-Jira inference (story e622): a CREATE that carried NO recorded channel
    # provisionally projected "unknown" above. When (and only when) the envelope bears
    # the legacy-Jira signature, infer a `jira` origin and mark it heuristic. A CREATE
    # that recorded a real channel is left untouched (guarded on the raw field).
    if data.get("creation_channel") is None:
        _project_legacy_creation_channel(state)
    # Identity entity payload (epic gnu-whale-ichor): an `identity` ticket's CREATE
    # carries email / mappings / keys. Surface them additively — present only when the
    # CREATE carried them, so a non-identity ticket's state is byte-for-byte unchanged
    # (mirrors the source_* handling above).
    for _id_key in ("email", "mappings", "keys"):
        _id_val = data.get(_id_key)
        if _id_val is not None:
            state[_id_key] = _id_val
    # Genesis keyring bootstrap (epic gnu-whale-ichor): an identity whose CREATE carried a
    # static `keys` list seeds one position-based keyring record per key, each added at the
    # CREATE event's position so its add-commit resolves to the CREATE commit. A keyless
    # identity keeps the seeded empty keyring.
    _bootstrap_genesis_keyring(state, f"{event.get('timestamp')}-{event.get('uuid')}")
    return None


def _project_legacy_creation_channel(state: dict) -> None:
    """Infer Jira origin for a channel-less legacy CREATE.

    Only the immutable ``jira-`` ID, reconciler author, and environment combination changes
    unknown to ``jira`` and records an inference marker. Other envelopes remain unknown. The
    helper reads no mutable fields and also supports snapshot migration.
    """
    ticket_id = state.get("ticket_id")
    if (
        isinstance(ticket_id, str)
        and ticket_id.startswith("jira-")
        and state.get("author") == LEGACY_JIRA_AUTHOR
        and state.get("env_id") == LEGACY_JIRA_ENV_ID
    ):
        state["creation_channel"] = "jira"
        state["creation_channel_inferred"] = True
    else:
        state["creation_channel"] = "unknown"


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
    # Denormalized author attribution (epic gnu-whale-ichor): present-only on the entry.
    _fold_author_attribution(_entry, event)
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
    """Append a LINK dependency after optional target resolution.

    When no tracker exists or resolution fails, retain the original target. Readiness treats an
    unresolved ``depends_on`` target as a closed tombstone because archived and invalid targets
    cannot be distinguished. Treating both as open would leave archived blockers active. See
    ``_status._get_ticket_status`` and its unresolved-blocker regression test.
    """
    raw_target = data.get("target_id", data.get("target", ""))
    resolved_target = raw_target
    if tracker_dir and raw_target:
        try:
            # Import DOWN from the stdlib-only leaf (mirrors the compute_alias
            # import above): the resolution primitive lives in rebar._ids, so the
            # pure replay layer never reaches UP into a higher read layer.
            from rebar._ids import resolve_ticket_id

            canonical = resolve_ticket_id(raw_target, tracker_dir)
            if canonical:
                resolved_target = canonical
        except Exception:  # noqa: BLE001 — resolver is best-effort; never crash the reducer
            pass
    relation = data.get("relation", "")
    dep_entry: dict = {
        "target_id": resolved_target,
        "relation": relation,
        "link_uuid": event["uuid"],
    }
    # caused_by provenance marker (ticket 6536-367c): surfaced present-only — exactly the
    # comment source_* pattern — so pre-marker events keep their prior dep shape and read
    # as unknown.
    provenance = data.get("provenance")
    if provenance is not None:
        dep_entry["provenance"] = provenance
    state["deps"].append(dep_entry)
    # Managed-ref provenance (safe-luge-nog): record the logical reference so a
    # later UNLINK can propagate a peer delete (process_unlink never removes it).
    add_managed_ref(state, relation, resolved_target)


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
    """Append a REVERT record and undo a reverted ARCHIVED projection.

    Reverting ARCHIVED clears the marker projection and reopens an archived status. Deleted
    tickets keep both deleted status and archival, preventing their return to list output.
    """
    _revert_record = {
        "uuid": event_uuid,
        "target_event_uuid": data.get("target_event_uuid"),
        "target_event_type": data.get("target_event_type"),
        "reason": data.get("reason", ""),
        "timestamp": event.get("timestamp"),
        "author": event.get("author"),
    }
    # Denormalized author attribution (epic gnu-whale-ichor): present-only on the record.
    _fold_author_attribution(_revert_record, event)
    state["reverts"].append(_revert_record)
    if (
        data.get("target_event_type") == "ARCHIVED"
        and state.get("archived")
        and state.get("status") != "deleted"
    ):
        state["archived"] = False
        if state.get("status") == "archived":
            state["status"] = "open"


# Genesis-provenance fields an EDIT event may NEVER overwrite (story 6fe2): the
# creation channel and its (later-story) inference marker are stamped once at CREATE
# and are immutable, so `process_edit` skips them even if a (buggy/malicious) EDIT
# names them. Other specialized processors never assign these fields.
_IMMUTABLE_EDIT_FIELDS = frozenset({"creation_channel", "creation_channel_inferred", "detected_by"})


def process_edit(state: dict, data: dict) -> None:
    """Apply an EDIT event: merge data.fields into state (last-writer-wins).

    Tags stored as comma-separated string in event; convert to list.
    If the value is already a list (e.g. from a SNAPSHOT), keep it.
    Unknown field names (not present in state) are silently ignored.

    Immutable genesis provenance (``_IMMUTABLE_EDIT_FIELDS``) is skipped so an EDIT can
    never overwrite the ``creation_channel`` / ``creation_channel_inferred`` set at CREATE.
    """
    fields = data.get("fields", {})
    for field_name, new_value in fields.items():
        if field_name not in state:
            continue
        if field_name in _IMMUTABLE_EDIT_FIELDS:
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
            # Managed-ref provenance (safe-luge-nog): re-parenting via EDIT (incl. an
            # inbound-ADOPTED parent the reconciler applies) makes the new parent a
            # reference we manage — fold it so a later detach can propagate. A detach
            # (parent_id -> None) folds nothing and never removes (monotonic).
            if field_name == "parent_id":
                add_managed_ref(state, "parent", new_value)


def _file_impact_scope(data: dict, paths: list) -> tuple[str, str]:
    """Derive the mutually-exclusive file-impact declaration from one event."""
    if paths:
        return "paths", ""
    if data.get("file_impact_scope") == "none":
        reason = data.get("no_file_impact_reason")
        return "none", reason if isinstance(reason, str) else ""
    return "undeclared", ""


def process_file_impact(state: dict, _event: dict, data: dict) -> None:
    """Apply a FILE_IMPACT event: replace the tri-state declaration (LWW)."""
    paths = data.get("file_impact") or []
    scope, reason = _file_impact_scope(data, paths)
    state["file_impact"] = paths
    state["file_impact_scope"] = scope
    state["no_file_impact_reason"] = reason


def process_verify_commands(state: dict, _event: dict, data: dict) -> None:
    """Replace ``verify_commands`` by LWW and map missing or null input to an empty list.

    This matches jq and FILE_IMPACT reduction, allowing list, search, and SNAPSHOT compaction to
    retain the commands.
    """
    state["verify_commands"] = data.get("verify_commands") or []


def process_workflow_run(state: dict, _event: dict, data: dict) -> None:
    """LWW-fold a complete workflow run by ``run_id``.

    Deterministic HLC and UUID replay selects the final record for each run on every clone. Lazy
    map creation preserves the shape of tickets without runs, and per-key replacement keeps
    other runs.
    """
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return
    runs = state.setdefault("workflow_runs", {})
    runs[run_id] = dict(data)


def process_workflow_step(state: dict, _event: dict, data: dict) -> None:
    """LWW-fold a completed step record by run and frame key.

    The event records the post-effect result and captured nondeterminism. Loop and map iterations
    use distinct frame paths, while flat and legacy events use ``step_id``. Deterministic replay
    lets retries replace earlier records without changing unrelated steps.
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


def process_commits(state: dict, _event: dict, data: dict) -> None:
    """Union attached commit records by SHA into ``state.commits``.

    Each input is a SHA string or record. The first replay occurrence wins for each SHA.
    Deterministic replay yields a convergent list. Lazy state survives snapshots and is excluded
    from Jira projection.
    """
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
    """Apply tag removals before additions.

    Per-event deltas preserve concurrent additions without whole-field EDIT replacement.
    Deterministic replay orders conflicts, and removal before addition gives additions precedence
    within one event. Repeated folds are idempotent. Non-list deltas act as empty lists, and
    legacy ``EDIT.tags`` remains the base.
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

    # FILE_IMPACT tri-state migration: a pre-feature snapshot carries only the
    # legacy array. Derive its conservative state after restoring that array, but
    # leave every post-feature snapshot's explicitly recorded pair untouched.
    if "file_impact_scope" not in compiled_state and "no_file_impact_reason" not in compiled_state:
        scope, reason = _file_impact_scope({}, state.get("file_impact") or [])
        state["file_impact_scope"] = scope
        state["no_file_impact_reason"] = reason

    if "plan_review_phase" not in compiled_state:
        status = state.get("status")
        if status not in _KNOWN_TICKET_STATUSES:
            raise ValueError(f"unknown ticket status in snapshot: {status!r}")
        phase = "planning" if status in ("open", "idea") else "execution"
        state["plan_review_phase"] = phase
        record = {
            "event": "plan_review_phase_bootstrap",
            "ticket_id": state.get("ticket_id"),
            "compiled_status": status,
            "phase": phase,
        }
        logger.info("plan review phase bootstrapped: %s", record, extra=record)

    # Holder provenance needs no migration because missing fields retain their initial ``None``.
    # Missing ``managed_refs`` instead seed from restored parent and dependency state. Later
    # LINK, UNLINK, and EDIT events continue folding the set.
    if "managed_refs" not in compiled_state:
        state["managed_refs"] = seed_managed_refs_from_current(state)

    # A snapshot without ``attestations`` folds its kind-bearing legacy signature into the map.
    # Blank or unkindable records add nothing. Later SIGNATURE events continue normal folding.
    if "attestations" not in compiled_state:
        sig = state.get("signature")
        if isinstance(sig, dict):
            kind = attestation_kind(sig.get("manifest"), {})
            if kind is not None:
                state.setdefault("attestations", {})[kind] = sig

    # Replace missing or epoch-based keyrings with position records. Remove ``keyring_epoch`` and
    # seed static identity keys at the CREATE position. Records with ``added_at`` need no migration.
    _restored_ring = state.get("keyring") or []
    _stale_epoch_era = any(
        isinstance(rec, dict) and "added_at" not in rec for rec in _restored_ring
    )
    if "keyring" not in compiled_state or _stale_epoch_era:
        state.pop("keyring_epoch", None)
        _bootstrap_genesis_keyring(state, str(state.get("created_at") or ""))

    # A legacy snapshot has no CREATE event to replay. Run the shared envelope heuristic only for
    # an absent, None, or unmarked ``unknown`` channel. Preserve recorded and inferred channels
    # because unconditional inference could replace them with ``unknown``.
    _channel = compiled_state.get("creation_channel")
    if _channel in (None, "unknown") and not compiled_state.get("creation_channel_inferred"):
        _project_legacy_creation_channel(state)
