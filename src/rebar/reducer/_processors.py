"""Event-type processors for the ticket reducer.

Each function takes the current mutable state dict, the parsed event dict,
and any ancillary data needed (e.g. filepath for conflict recording), and
applies the event's effect to state in-place.  All processors return None.
"""

from __future__ import annotations

import json
import logging
import os

from ._managed_refs import add_managed_ref, seed_managed_refs_from_current

logger = logging.getLogger(__name__)


def _fold_author_attribution(target: dict, event: dict) -> None:
    """Surface denormalized author attribution PRESENT-ONLY (epic gnu-whale-ichor).

    Copies ``author_email`` / ``author_id`` from the event ENVELOPE onto ``target``
    only when the event carries them — so a pre-change event (no such keys) reduces to
    byte-identical state and no new keys appear anywhere. Mirrors the ``source_*``
    present-only handling. ``target`` is top-level state for a CREATE, or a per-entry
    record (comment / revert / signature) for the other stamping sites.
    """
    for _key in ("author_email", "author_id"):
        _val = event.get(_key)
        if _val is not None:
            target[_key] = _val


def _rederive_keyring_keys(state: dict) -> None:
    """Re-derive ``state['keys']`` from the keyring so existing ``keys`` readers
    (authorship trust root, show) keep working: the CURRENTLY-valid keys are the public
    keys of records with ``revoked_at is None``. Preserves keyring order; skips malformed
    records defensively (epic gnu-whale-ichor — position-based keyring)."""
    keys: list[str] = []
    for rec in state.get("keyring") or []:
        if not isinstance(rec, dict):
            continue
        pub = rec.get("public_key")
        if isinstance(pub, str) and pub and rec.get("revoked_at") is None:
            keys.append(pub)
    state["keys"] = keys


def process_key_event(state: dict, event: dict, data: dict, event_type: str) -> None:
    """Apply a KEY_ADD / KEY_REVOKE event to an identity's POSITION-based keyring
    (epic gnu-whale-ichor — the git-commit-ancestry validity model).

    A keyring record is ``{public_key, added_at: <position>, revoked_at: <position|None>}``
    where a POSITION is the event's ``{timestamp}-{uuid}`` filename prefix — the immutable
    anchor a verifier later resolves to the introducing tickets-branch commit. There is NO
    epoch cursor: the event's own position IS the ordinal.

    * ``KEY_ADD``  — append ``{public_key, added_at: <this event's position>, revoked_at:
      None}``.
    * ``KEY_REVOKE`` — set ``revoked_at = <this event's position>`` on the first matching
      STILL-VALID record (``public_key`` matches and ``revoked_at is None``); a revoke naming
      an unknown / already-revoked key folds no change.

    After folding, ``state['keys']`` is re-derived from the currently-valid records so
    downstream ``keys`` consumers are unaffected by the keyring representation.
    """
    public_key = data.get("public_key")
    keyring = state.setdefault("keyring", [])
    position = f"{event.get('timestamp')}-{event.get('uuid')}"
    if event_type == "KEY_ADD":
        if isinstance(public_key, str) and public_key:
            keyring.append({"public_key": public_key, "added_at": position, "revoked_at": None})
    elif event_type == "KEY_REVOKE":
        if isinstance(public_key, str) and public_key:
            for rec in keyring:
                if (
                    isinstance(rec, dict)
                    and rec.get("public_key") == public_key
                    and rec.get("revoked_at") is None
                ):
                    rec["revoked_at"] = position
                    break
    _rederive_keyring_keys(state)


def _bootstrap_genesis_keyring(state: dict, create_position: str) -> None:
    """Seed an ``identity``'s keyring from a static ``keys`` list carried on its CREATE
    (epic gnu-whale-ichor). Every genesis key is recorded as added at ``create_position``
    (the CREATE event's ``{timestamp}-{uuid}`` prefix), so a genesis key's add-commit
    resolves to the CREATE commit — NOT a magic sentinel. An identity created with NO keys
    keeps ``keyring=[]`` (its first KEY_ADD is then the TOFU add), so both pre-existing
    (keys-at-create) and new identities converge on the same position model."""
    keys = state.get("keys")
    if state.get("ticket_type") != "identity" or not isinstance(keys, list) or not keys:
        return
    ring = [
        {"public_key": k, "added_at": create_position, "revoked_at": None}
        for k in keys
        if isinstance(k, str) and k
    ]
    if ring:
        state["keyring"] = ring


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
    # Creation-channel provenance (epic jira-reb-977, story 6fe2): the public ingress
    # (cli/mcp/python/jira/import) that produced this genesis CREATE. Projected
    # UNCONDITIONALLY — a post-feature CREATE always carries it; a LEGACY CREATE with no
    # field provisionally projects "unknown" (the projection-only fallback). We do NOT
    # set `creation_channel_inferred` here (a later story owns legacy inference); this is
    # a provisional projection only. `process_edit` guards both keys against overwrite so
    # genesis provenance is immutable.
    state["creation_channel"] = data.get("creation_channel", "unknown")
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


def _fold_claimed_session(state: dict, data: dict) -> None:
    """Record the claiming session id on a winning ``open -> in_progress`` STATUS fold.

    Epic crust-fetch-stump, story 68ef. ``claimed_session`` records the session that
    performed the CURRENT ``open -> in_progress`` claim (mirrors ``assignee``). On that
    edge we set it to ``data.get("session")`` — the id the write side stamped, or ``None``
    when the claim carried no session. Setting to ``None`` on a session-less re-claim
    deliberately CLEARS any stale prior id, so the field never mis-attributes the current
    in_progress episode to a past session (advisory T9). Keyed on the ``open -> in_progress``
    edge only, so a later ``blocked -> in_progress`` (etc.) leaves it untouched.

    It is applied ONLY when this event's status is being applied — the caller invokes it in
    the normal-update and fork-WINNER branches, never where the existing chain wins — so a
    losing concurrent claim never overwrites the winner's session (advisory G6/T8). Older
    clones ignore the additive ``data["session"]`` key (forward-compatible).
    """
    if data.get("current_status") == "open" and data.get("status") == "in_progress":
        state["claimed_session"] = data.get("session")
        # Multi-harness provenance (story c557): fold the harness tag and secondary remote
        # session on the same edge, with the same fork-winner + session-less-clear semantics.
        state["claim_harness"] = data.get("harness")
        state["claim_remote_session"] = data.get("remote_session")


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

    # Capture the pre-update status so we can detect a closed->open reopen below.
    prev_status = state.get("status")

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
            _fold_claimed_session(state, data)  # only when THIS (winning) event is applied
        else:
            # Existing chain wins; keep state as-is.
            winner_uuid = existing_uuid
            loser_uuid = incoming_uuid
            loser_env_id = event.get("env_id", "") or ""

        ticket_id = state.get("ticket_id", "")
        logger.warning(
            "PARENT_CHAIN_FORK_RESOLVED ticket=%s winner=%s dropped=[%s] loser_env_id=[%s]",
            ticket_id,
            winner_uuid,
            loser_uuid,
            loser_env_id,
        )
        # Record the resolved fork in PURE derived state (rebuilt identically on every
        # replay — no external I/O) so a concurrent claim/status race becomes discoverable
        # via fsck/show after the fact (audit reliability #1, story 3003). loser_env_id is
        # intentionally NOT stored: it is unreliable across reopens, and claim-loss
        # detection uses the authoritative `assignee` field instead (see _commands/claim).
        state.setdefault("status_fork_resolutions", []).append(
            {"winner_uuid": winner_uuid, "dropped_uuid": loser_uuid}
        )
    else:
        state["status"] = data.get("status", state["status"])
        _fold_claimed_session(state, data)  # normal (non-fork) update — this event is applied
        # Advance to THIS event's OWN UUID (not its data parent-pointer) so a
        # subsequent concurrent sibling forks against this event's identity and
        # resolves by the lexical-UUID rule above — deterministically and
        # independent of replay order, exactly as this docstring / docs/concurrency.md
        # describe. Bug 8874: the previous `data["parent_status_uuid"]` stored the
        # common-parent pointer, so two siblings from an EMPTY parent compared the
        # incoming uuid against "" and the later-replayed event won by insertion
        # order rather than by UUID. Matches the fork branch above, which already
        # records the winner's own UUID.
        state["parent_status_uuid"] = event.get("uuid") or ""
        state["last_status_env_id"] = event.get("env_id") or ""

    # Record the most recent closed->open (reopen) transition timestamp (epic
    # dark-acme-lumen). Validity-on-read uses it to invalidate a completion/plan-review
    # attestation signed BEFORE a reopen, without mutating the immutable attestation record.
    # Set only on the closed->open edge (applies to both the fork and normal branches via the
    # resolved status); left absent for tickets that were never reopened.
    if prev_status == "closed" and state.get("status") == "open":
        state["last_reopened_at"] = event.get("timestamp")


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
    state["deps"].append(
        {
            "target_id": resolved_target,
            "relation": relation,
            "link_uuid": event["uuid"],
        }
    )
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
_IMMUTABLE_EDIT_FIELDS = frozenset({"creation_channel", "creation_channel_inferred"})


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


def attestation_kind(manifest: list | None, data: dict) -> str | None:
    """Derive the attestation kind used to key ``state['attestations']``.

    The SIGNED ``manifest[0]`` is authoritative: the kind is the substring before the
    first ``":"`` (e.g. ``"plan-review: PASS"`` -> ``"plan-review"``,
    ``"completion-verifier: PASS"`` -> ``"completion-verifier"``). ``data['kind']`` is an
    UNSIGNED routing hint — it is never allowed to override the signed manifest, so a
    mismatched hint is ignored and the manifest-derived kind is used. Returns None for a
    blank/retired or otherwise unkindable manifest (no first line, or no ``":"``); such an
    event stays OUT of the map (it cannot key a kind)."""
    if not manifest:
        return None
    first = str(manifest[0])
    if ":" not in first:
        return None
    derived = first.split(":", 1)[0].strip() or None
    if derived is None:
        return None
    # ``data['kind']`` is an UNSIGNED routing hint. The signed manifest is authoritative, so
    # we consult the hint only to honor/validate it: a hint that disagrees with the
    # manifest-derived kind is IGNORED (the manifest wins) — a forged/buggy envelope kind can
    # never misroute a signed attestation. Either way the manifest-derived kind is returned.
    hint = data.get("kind")
    if hint is not None and str(hint).strip() != derived:
        return derived
    return derived


def process_signature(state: dict, event: dict, data: dict) -> None:
    """Apply a SIGNATURE event: maintain the most-recent ``state['signature']`` mirror
    AND, additively, file the record under its kind in ``state['attestations']``.

    The MIRROR keeps the exact prior single-slot last-writer-wins behavior — EVERY event
    (including a blank/retired one) replaces ``state['signature']`` — so the existing
    ``state.get('signature')`` consumers (verify, the close gate, fsck) are unchanged by this
    slice, and the SNAPSHOT/rollback mirror is automatic (the compactor
    builds compiled_state via this reducer).

    The MAP (``state['attestations']``, epic dark-acme-lumen) is purely additive: a kindable
    event sets ``attestations[kind]`` (per-key last-writer-wins, so re-signing one kind
    replaces only that kind and the others survive — fixing the cross-kind clobber). A
    blank/retired/unkindable event is SKIPPED for the map (it cannot key a kind; its staleness
    is handled later by validity-on-read, not by clobbering). Kind comes from the signed
    ``manifest[0]`` (``data['kind']`` is only a validated hint). The signed-at timestamp falls
    back to the event timestamp for forward-compat records.
    """
    _manifest = data.get("manifest")
    # Coerce to a list: never persist a non-list truthy value (e.g. a dict) into reduced
    # state, which would leak a malformed shape into show/MCP output (fail closed).
    manifest = _manifest if isinstance(_manifest, list) else []
    kind = attestation_kind(manifest, data)
    record = {
        "manifest": manifest,
        "algorithm": data.get("algorithm"),
        "signature": data.get("signature"),
        "key_id": data.get("key_id"),
        "head_sha": data.get("head_sha"),
        "signed_at": data.get("signed_at") or event.get("timestamp"),
        "author": event.get("author"),
        # The resolved (manifest-authoritative) kind, so the record is self-describing —
        # esp. for the legacy mirror, which has no map-key context. None for a
        # blank/retired/unkindable event.
        "kind": kind,
    }
    # Denormalized author attribution (epic gnu-whale-ichor): present-only on the record.
    _fold_author_attribution(record, event)
    # Asymmetric op-cert fields (keystone e4df): folded PRESENT-ONLY, so an HMAC event (no
    # ``envelope`` in data) reduces to a byte-identical record and these keys never appear on it.
    if data.get("envelope") is not None:
        record["envelope"] = data["envelope"]
        record["material_fingerprint"] = data.get("material_fingerprint")
        record["merged_log_commit"] = data.get("merged_log_commit")
        # The DSSE principal (env_id) the op-cert was signed under (story 8d8e), so the shape-aware
        # verify wrapper can classify a foreign-environment cert without decoding the envelope.
        record["principal"] = data.get("principal")
    # Mirror: unchanged single-slot semantics (back-compat for existing consumers).
    state["signature"] = record
    # Map: additive, kind-keyed; skip blank/retired/unkindable events (no key derivable).
    if kind is not None:
        state.setdefault("attestations", {})[kind] = record


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

    # claimed_session (epic crust-fetch-stump, story 199b) needs NO active snapshot guard:
    # a POST-feature snapshot carries the key and it is restored verbatim above, and a
    # PRE-feature snapshot's compiled_state simply lacks it, leaving the make_initial_state
    # seed (None) intact. Unlike managed_refs (seeded) / attestations (folded), there is no
    # migration to perform — the round-trip regression test pins both directions.
    # Managed-ref provenance migration (safe-luge-nog): a SNAPSHOT written before
    # this field existed carries no ``managed_refs``, so restoring it would leave the
    # projection empty and silently disable removal-propagation for the ticket's
    # existing refs (the compaction durability hole). Seed it from the restored
    # current parent_id + deps so those refs are treated as managed. Post-feature
    # SNAPSHOTs DO carry managed_refs and are restored verbatim above (this is a
    # no-op for them). Post-snapshot LINK/UNLINK/EDIT events replay afterwards and
    # fold in normally.
    if "managed_refs" not in compiled_state:
        state["managed_refs"] = seed_managed_refs_from_current(state)

    # Attestations fold-in (epic dark-acme-lumen): an OLD snapshot (written before the
    # kind-keyed map existed) carries only the legacy single `signature` and no
    # `attestations`. Fold that record into the map under its manifest-derived kind so
    # kind-keyed consumers see it; a blank/unkindable legacy record is dropped (no sentinel).
    # Post-snapshot SIGNATURE events replay into the map normally. A post-feature snapshot
    # already carries `attestations` and is restored verbatim above (this is a no-op).
    if "attestations" not in compiled_state:
        sig = state.get("signature")
        if isinstance(sig, dict):
            kind = attestation_kind(sig.get("manifest"), {})
            if kind is not None:
                state.setdefault("attestations", {})[kind] = sig

    # Keyring migration (epic gnu-whale-ichor — position-based keyring, SCHEMA_VERSION 5):
    # a SNAPSHOT written before the position-based keyring existed either carries no
    # `keyring` at all, OR carries stale epoch-era records ({added_epoch, revoked_epoch}
    # with no `added_at`) plus a `keyring_epoch` cursor. In BOTH cases do NOT trust the
    # stale fields: drop the legacy cursor and re-seed a genesis keyring from the identity's
    # static `keys` (every key added at the CREATE position), so position-based verification
    # keeps working. A post-feature SNAPSHOT carries position-based records (each with
    # `added_at`) and is restored verbatim above (a no-op here).
    _restored_ring = state.get("keyring") or []
    _stale_epoch_era = any(
        isinstance(rec, dict) and "added_at" not in rec for rec in _restored_ring
    )
    if "keyring" not in compiled_state or _stale_epoch_era:
        state.pop("keyring_epoch", None)
        _bootstrap_genesis_keyring(state, str(state.get("created_at") or ""))
