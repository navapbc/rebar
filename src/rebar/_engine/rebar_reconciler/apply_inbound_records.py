#!/usr/bin/env python3
"""Phase helpers for the inbound create/update leaf appliers.

Extracted from ``apply_inbound.py`` (module-size split, ticket 090a): that module
sat at exactly the 800-LOC soft cap, so decomposing ``_apply_inbound_create`` and
``_apply_inbound_update`` into orchestrator + phase helpers IN-FILE would have
breached the cap. The two orchestrators stay in ``apply_inbound.py`` (so
``apply_inbound._apply_inbound_create`` / ``_apply_inbound_update`` remain the
public names imported by ``applier.py`` / ``typed_dispatch.py``); the cohesive
per-phase bodies they call in order live here.

Each helper holds the former inline body VERBATIM — same computations, same order,
same event writes. The helpers append to a caller-owned ``written`` list (update)
or return the values the orchestrator threads onward (create). Imports only
downward (``batch_dispatch`` / ``inbound_translate``); never imports
``apply_inbound`` (so the orchestrator can import these helpers back without a
cycle).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from rebar_reconciler.batch_dispatch import _call_with_retry
from rebar_reconciler.inbound_translate import (
    _BRIDGE_INTERNAL_TAG_PREFIXES,
    _JIRA_TYPE_MAP,
    _REBAR_STATUS_LABEL_TO_LOCAL,
    _extract_name,
    _jira_key_to_local_id,
    _jira_status_to_local,
    _load_ticket_reducer,
    _normalize_adf_body,
    _read_latest_status,
    _resolve_priority,
    _resolve_tracker_dir,
    _write_event_file,
)

logger = logging.getLogger(__name__)

# Loop-breaker marker (mirrors inbound_differ.RECONCILER_MARKER). Comments whose
# body carries this marker are our own outbound echoes, so the comment-history
# bootstrap in _apply_inbound_create — can skip our own echoes.
_RECONCILER_MARKER_APPLIER = "<!-- rebar:reconciler-echo -->"


def _ensure_inbound_assignee_identity(assignee, repo_root) -> None:
    """Best-effort: mint/reuse a placeholder identity for an inbound Jira assignee
    (2f13). When the assignee field carries an ``accountId``, resolve it through
    :func:`rebar.ensure_identity_for` (provider ``"jira"``, keyed on the opaque
    accountId) so an unmapped inbound user gets a ghost identity that a later
    outbound pass can key on.

    ADDITIVE + best-effort: this NEVER changes the human-readable name extraction and
    NEVER fails the inbound apply — no ``accountId`` or any mint failure is swallowed
    and the apply continues with the name-only behavior."""
    if not isinstance(assignee, dict):
        return
    account_id = assignee.get("accountId")
    if not (isinstance(account_id, str) and account_id.strip()):
        return
    display_name = _extract_name(assignee)
    try:
        import rebar

        rebar.ensure_identity_for(
            "jira",
            account_id,
            display_name or account_id,
            repo_root=repo_root,
            creation_channel="jira",
        )
    except Exception:  # noqa: BLE001 — best-effort ghost mint; never fail the inbound apply
        logger.debug(
            "inbound: could not ensure identity for jira accountId %r", account_id, exc_info=True
        )


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
    # Parent sync (ticket 8b25): resolve the Jira parent field to a local id.
    # Three sources, in priority order:
    #   1. payload["_parent_local_id"] — pre-resolved by reconcile.py for the
    #      normal inbound-create path (reconcile already has binding_store).
    #   2. fields["parent"]["key"] derived via _jira_key_to_local_id —
    #      best-effort local-id derivation for jira-originated parents where the
    #      local id is deterministic (jira-dig-N convention).
    #   3. Empty string — safe fallback (hardcoded prior behaviour).
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
        # Live snapshots carry description as a raw ADF dict — normalize to
        # plain text or the CREATE event stores a dict where the reducer
        # expects a string (bug 1bb2 class), and the outbound differ then
        # re-pushes the description on EVERY pass (dict never equals the
        # ADF-decoded snapshot text). Ticket robe-creek-zealot.
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
        _ensure_inbound_assignee_identity(fields["assignee"], repo_root)
    # Creation-channel provenance (story e622): this inbound Jira CREATE is written
    # DIRECTLY (bypassing composer.create_core), so we stamp the channel here. Validate
    # first so the direct writer honours the same closed-vocabulary contract create_core
    # enforces. This is a RECORDED value (not a heuristic inference), so we deliberately
    # do NOT set creation_channel_inferred.
    from rebar.reducer._version import validate_creation_channel

    create_data["creation_channel"] = validate_creation_channel("jira")
    create_path = _write_event_file(tracker_dir, local_id, "CREATE", create_data)
    return tracker_dir, raw_labels, create_path


def _inbound_create_write_status_event(fields, raw_labels, tracker_dir, local_id) -> None:
    """Phase: write a STATUS event when the Jira status reverse-maps to non-default."""
    # Status: write a STATUS event when the Jira status reverse-maps to
    # something other than the reducer default ('open'). A rebar-status:
    # annotation label takes precedence over the raw workflow status —
    # same precedence as inbound_differ._map_jira_to_local_fields, so the
    # import lands at the status the bound-ticket differ would compute.
    local_status: str | None = None
    for _lbl in raw_labels:
        if isinstance(_lbl, str) and _lbl in _REBAR_STATUS_LABEL_TO_LOCAL:
            local_status = _REBAR_STATUS_LABEL_TO_LOCAL[_lbl]
            break
    if local_status is None:
        jira_status = _extract_name(fields.get("status"))
        if jira_status:
            local_status = _jira_status_to_local(jira_status)
    if local_status and local_status != "open":
        _write_event_file(
            tracker_dir,
            local_id,
            "STATUS",
            {"status": local_status, "current_status": "open"},
        )


def _inbound_create_record_binding(mutation, binding_store, local_id, jira_key) -> None:
    """Phase: record the local<->jira binding + seed the adopt baseline."""
    # Record the local<->jira binding NOW — we hold both ids. Without this,
    # the outbound differ (whose bound/unbound signal is the binding store
    # ALONE) sees the freshly imported ticket as unbound on the next pass and
    # re-emits an outbound CREATE for it, converging only via create_one's
    # dedup-skip JQL search — one wasted create + per-ticket Jira search per
    # imported ticket (ticket robe-creek-zealot, 1-pass idempotency).
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


def _inbound_create_writeback_jira(client, jira_key, local_id, tracker_dir) -> None:
    """Phase: write identity markers + bootstrap pre-existing comments back to Jira."""
    # Write rebar-id label + local_id entity property back to Jira so the
    # differ recognizes this issue as mirrored on subsequent passes (dedup).
    if client is not None:
        _call_with_retry(client.add_label, jira_key, f"rebar-id:{local_id}")
        _call_with_retry(client.set_entity_property, jira_key, "local_id", local_id)

    # Bug 221b: bootstrap pre-existing Jira comments so the local ticket has
    # a complete comment history immediately after inbound create.
    #
    # Strategy (mirrors _diff_comments_inbound in inbound_differ.py):
    #   1. Fetch the issue's comments via client.get_comments(jira_key).
    #   2. Skip any comment whose body (ADF-normalized) contains the
    #      loop-breaker marker — those are our own outbound echoes.
    #   3. Normalize ADF bodies to plain text (via _normalize_adf_body).
    #   4. Write one COMMENT event per remaining comment, recording
    #      jira_comment_id so the next-pass inbound comment diff dedupes.
    #
    # On get_comments failure: log a warning and skip comment bootstrap —
    # the CREATE still succeeds.
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
    # title needs a fallback (the legacy Jira-keyed ``summary``), so it stays inline;
    # status (a 2nd STATUS event) and labels (tag read-modify-write) are special-cased
    # below. The remaining scalar fields are a declarative {field: Jira→local transform}
    # table — adding a synced scalar field is now a one-line entry. Notes preserved:
    #   - description: normalize a raw ADF dict → plain text (Bug 1bb2 defense-in-depth;
    #     the differ should normalize at read time, but a bypassing caller may forward ADF).
    #   - parent_id: an absent/empty parent maps to "" (clears the parent; ticket 8b25,
    #     surfaced by the inbound differ as ``fields["parent_id"] = <local_id>``).
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

    # 2f13 (additive): mint/reuse a ghost identity for the inbound assignee when it
    # carries an opaque accountId — best-effort, never fails the update.
    if "assignee" in fields:
        _ensure_inbound_assignee_identity(fields["assignee"], repo_root)

    if edit_fields:
        path = _write_event_file(tracker_dir, local_id, "EDIT", {"fields": edit_fields})
        written.append(str(path))


def _inbound_update_write_status_event(fields, tracker_dir, local_id, written) -> None:
    """Phase: write a STATUS event when the payload carries a Jira status change."""
    if "status" in fields:
        raw_status = fields["status"]
        # Bug 1bb2 (llm-review finding 2): Trust the differ contract by
        # SHAPE rather than VALUE. The inbound differ
        # (_diff_jira_vs_local → _map_jira_to_local_fields) always emits
        # status as a pre-mapped local string. Legacy callers that bypass
        # the differ pass the raw Jira shape (a dict like {"name": "..."}).
        # A value-membership check would mis-route a Jira tenant whose
        # status is literally named one of the local values (e.g.
        # 'in_progress') — but only the dict shape needs reverse-mapping,
        # so the type itself is a reliable discriminator.
        if isinstance(raw_status, dict):
            local_status = _jira_status_to_local(_extract_name(raw_status))
        else:
            local_status = raw_status
        # current_status is the PREVIOUS state (matched against state["status"]
        # by the reducer for fork detection — see
        # ticket_reducer/_processors.py:process_status).
        # Read the latest STATUS event from the ticket dir to obtain it.
        previous_status = _read_latest_status(tracker_dir, local_id)
        path = _write_event_file(
            tracker_dir,
            local_id,
            "STATUS",
            {"status": local_status, "current_status": previous_status},
        )
        written.append(str(path))


def _inbound_update_apply_labels(mutation, payload, tracker_dir, local_id, written) -> None:
    """Phase: apply inbound label add/remove ops as a convergent TAG_DELTA event."""
    # Bug 57b0: inbound labels — apply payload['labels'] add/remove ops as
    # an EDIT event on `fields.tags`. The inbound differ surfaces label
    # mutations under ``payload['labels']`` as
    # ``[{"action": "add"|"remove", "label": "<name>"}]`` (see
    # inbound_differ._diff_labels_inbound). Without this block, every
    # inbound label add/remove was silently dropped — Jira-side label
    # changes never propagated to local tags.
    #
    # The local reducer treats tags as REPLACE-on-EDIT (see
    # ticket_reducer/_processors.process_edit), so we must read the
    # current tag list, apply the diff, and write the full resulting list.
    inbound_labels = payload.get("labels") or []
    if isinstance(inbound_labels, list) and inbound_labels:
        # Read current tags by reducing the existing ticket directory.
        #
        # Bug bc8f-775e-9a34-44d1: the local reducer treats `fields.tags` on
        # an EDIT event as REPLACE-the-whole-list (see
        # ticket_reducer/_processors.process_edit). If we cannot reliably
        # read the current tag list — either because reduce_ticket raises
        # OR because it returns None (ticket dir doesn't exist yet, e.g.,
        # race with a concurrent CREATE) — falling back to `current_tags=[]`
        # and writing `EDIT(fields.tags=[<just the new label>])` would WIPE
        # every pre-existing local tag. The live probe captured exactly
        # this: ticket b2e9 with `labelprobe-...` was reduced to `[]` after
        # T1's bidirectional pass.
        #
        # Safe behaviour: when current state cannot be read, SKIP the labels
        # EDIT for this pass and emit a stderr warning. The next reconciler
        # pass will retry once the ticket dir / state is readable.
        reducer_failed = False
        current_state: dict | None = None
        try:
            reducer_mod = _load_ticket_reducer()
            current_state = reducer_mod.reduce_ticket(str(tracker_dir / local_id))
        except Exception as _reducer_exc:  # noqa: BLE001 — see bc8f docstring
            reducer_failed = True
            print(
                f"[applier] WARN bc8f-guard: reducer raised while reading "
                f"current tags for {local_id} (target={mutation.target}); "
                f"skipping labels EDIT to avoid wiping local tags. "
                f"exc={type(_reducer_exc).__name__}: {_reducer_exc}",
                file=sys.stderr,
            )
        if not reducer_failed and current_state is None:
            print(
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
                # Bug a06c: mark this TAG_DELTA with source=inbound (at data.source)
                # so local_label_intent skips its added tags when building the
                # "user-intent" set. Without the marker, a Jira-side ADD applied
                # locally would look like user intent and a later Jira-side REMOVE
                # would be cancelled by a spurious outbound ADD (T4 IB-REMOVE).
                path = _write_event_file(
                    tracker_dir,
                    local_id,
                    TAG_DELTA,
                    {"added": added, "removed": removed, "source": "inbound"},
                )
                written.append(str(path))


def _inbound_update_apply_comments(payload, tracker_dir, local_id, written) -> None:
    """Phase: write a COMMENT event for each new Jira comment the differ surfaced."""
    # Bug 85a1 (Gap 1): inbound comments — write a COMMENT event for each
    # new Jira comment the differ surfaced. The body is stored as plain
    # text (post-ADF-decode); ``jira_comment_id`` is persisted so the
    # outbound differ's loop-breaker skips this comment on the next pass.
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


def _inbound_update_apply_links(payload, local_id, repo_root) -> int:
    """Phase: write each Jira-sourced relation into rebar via the rebar.link facade."""
    # Cycle 3: inbound links — write each Jira-sourced relation into rebar via
    # the rebar.link library facade. rebar.link owns relation validation,
    # hierarchy promotion, cycle/redundant-link guards, and the LINK event
    # write — so we do NOT hand-write LINK events here. The redundant-link
    # guard inside add_dependency makes re-apply idempotent. Failures are
    # non-fatal and logged.
    inbound_links = payload.get("links") or []
    links_applied: int = 0
    if isinstance(inbound_links, list):
        import rebar

        for entry in inbound_links:
            if not isinstance(entry, dict):
                continue
            if entry.get("action") != "add":
                continue
            target_local_id = entry.get("target_id")
            relation = entry.get("relation")
            if not target_local_id or not relation:
                continue
            try:
                rebar.link(local_id, target_local_id, relation, repo_root=repo_root)
                links_applied += 1
            except Exception as exc:  # noqa: BLE001 — fail-open: skip this link, continue applying others
                logger.warning(
                    "_apply_inbound_update: rebar.link failed for %s -> %s (%s): %r",
                    local_id,
                    target_local_id,
                    relation,
                    exc,
                )
    return links_applied
