#!/usr/bin/env python3
"""Inbound leaf appliers: materialise Jira issues into the local ticket store.

The eight (inbound, *) leaf handlers dispatched from the typed registry, plus
inbound_repair_property (the local_id property write _apply_inbound_repair_property
delegates to). Each leaf reads its Jira-side payload, translates it via
inbound_translate, and writes local ticket events; rebar-id/property write-backs
to Jira run through batch_dispatch._call_with_retry.

Imports downward only (apply_base, batch_dispatch, inbound_translate); never
imports applier.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from rebar_reconciler.apply_base import (
    ApplyResult,
    _direction_guard,
    _load_mutation_module,
)
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
from rebar_reconciler.pass_io import _write_mapping_atomic

logger = logging.getLogger(__name__)


def _rebar_env(name: str, default: str | None = None) -> str | None:
    """Read ``REBAR_<name>`` from the environment (module-local; see applier)."""
    return os.environ.get(f"REBAR_{name}", default)


# Loop-breaker marker (mirrors inbound_differ.RECONCILER_MARKER).
# Outbound comments embed this token so inbound passes — including the
# bootstrap in _apply_inbound_create — can skip our own echoes.
_RECONCILER_MARKER_APPLIER = "<!-- rebar:reconciler-echo -->"


def _apply_inbound_create(
    mutation, *, client=None, repo_root=None, binding_store=None
) -> ApplyResult:
    """Materialise a remote Jira issue as a local jira-* ticket.

    Writes a CREATE event (title, ticket_type, priority, description, tags
    including ``imported:reconciler-bootstrap``) and, when the payload carries
    a non-default status, a follow-up STATUS event reverse-mapped via
    config.local_to_jira_status.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    # Accept both shapes: payload with nested "fields" key (batch-dict shape)
    # and payload with top-level field keys (differ Mutation shape).
    fields = payload.get("fields") or payload
    jira_key = mutation.target
    local_id = _jira_key_to_local_id(jira_key)

    # Defense-in-depth inbound dedup (ticket 1577). The snapshot differ normally
    # stands down for an already-bound issue by recognising its rebar-id:<local_id>
    # label in the fetched fields (bug 4354) — that is the primary production
    # dedup. This guard covers the narrow transient where the snapshot predates
    # the label write-back yet the binding already exists in bindings.json: the
    # differ then mis-emits an inbound CREATE which would materialise a duplicate
    # local ticket. If the target Jira key is already bound, record the mapping
    # and skip materialisation. Cheap (local reverse-index lookup, no Jira GET).
    if binding_store is not None:
        bound_local_id = binding_store.get_local_id(jira_key)
        if bound_local_id:
            if repo_root is None:
                repo_root = Path(
                    os.environ.get("REBAR_ROOT")
                    or Path(__file__).resolve().parents[4]
                )
            mapping_path = repo_root / "bridge_state" / "mapping.json"
            _write_mapping_atomic(mapping_path, bound_local_id, jira_key)
            return ApplyResult(
                mutation.direction,
                mutation.action,
                {
                    "local_id": bound_local_id,
                    "jira_key": jira_key,
                    "dedup_skipped": True,
                },
            )

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
    create_path = _write_event_file(tracker_dir, local_id, "CREATE", create_data)

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
        else:
            import sys as _sys

            print(  # noqa: T201
                f"WARNING: inbound_create: binding store lacks bind_confirm; "
                f"{local_id!r}<->{jira_key!r} NOT bound at create — the next "
                f"pass will re-emit an outbound create (dedup-skip will "
                f"converge it).",
                file=_sys.stderr,
            )

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
        except Exception as exc:  # noqa: BLE001
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

    return ApplyResult(
        mutation.direction,
        mutation.action,
        {"local_id": local_id, "create_event": str(create_path)},
    )


def _apply_inbound_update(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Apply a remote-side update to an existing local jira-* ticket.

    Writes one EDIT event with the changed fields, plus an additional STATUS
    event when the payload includes a Jira status change. Unknown ticket
    directories are tolerated (the EDIT is still written; the reducer will
    surface fsck on the next read).
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    # Accept both shapes: payload with nested "fields" key (batch-dict shape)
    # and payload with top-level field keys (differ Mutation shape).
    fields = payload.get("fields") or payload
    target = mutation.target
    # Bug 1bb2: prefer payload['local_id'] (set by reconcile.py for bound
    # tickets) over Jira-key-derived local_id. Without this, EDIT events
    # for a bound UUID ticket are written under a new jira-dig-NNN/
    # directory — creating a duplicate ticket and silently dropping the
    # update on the bound UUID ticket.
    local_id = payload.get("local_id") or (
        target if target.startswith("jira-") else _jira_key_to_local_id(target)
    )
    tracker_dir = _resolve_tracker_dir(repo_root)

    # Map field names to local reducer field names. The inbound differ
    # ALREADY maps Jira → local (see inbound_differ._map_jira_to_local_fields:
    # emits ``title`` / ``ticket_type`` / local-mapped ``status``). Accept
    # the local-keyed shape from the differ AND the legacy Jira-keyed
    # ``summary`` for back-compat with any caller that bypasses the differ.
    edit_fields: dict[str, Any] = {}
    if "title" in fields:
        edit_fields["title"] = fields["title"]
    elif "summary" in fields:
        edit_fields["title"] = fields["summary"]
    if "description" in fields:
        desc = fields["description"]
        # Bug 1bb2: normalize ADF dict → plain text. The differ should
        # normalize at read time, but guard here too in case a caller
        # forwards the raw ADF dict (defense-in-depth).
        if isinstance(desc, dict):
            desc = _normalize_adf_body(desc)
        edit_fields["description"] = desc
    if "priority" in fields:
        edit_fields["priority"] = _resolve_priority(fields["priority"])
    if "assignee" in fields:
        edit_fields["assignee"] = _extract_name(fields["assignee"])
    if "ticket_type" in fields:
        edit_fields["ticket_type"] = fields["ticket_type"]
    # Parent sync (ticket 8b25): inbound parent_id change → include in EDIT.
    # The inbound differ surfaces this as ``fields["parent_id"] = <local_id>``.
    if "parent_id" in fields:
        edit_fields["parent_id"] = fields["parent_id"] or ""

    written: list[str] = []
    if edit_fields:
        path = _write_event_file(tracker_dir, local_id, "EDIT", {"fields": edit_fields})
        written.append(str(path))

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
            current_tags: list[str] = list(current_state.get("tags", []) or [])
            new_tags = list(current_tags)
            changed = False
            for entry in inbound_labels:
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action")
                label_name = entry.get("label", "")
                if not label_name or not isinstance(label_name, str):
                    continue
                if action == "add" and label_name not in new_tags:
                    new_tags.append(label_name)
                    changed = True
                elif action == "remove" and label_name in new_tags:
                    new_tags = [t for t in new_tags if t != label_name]
                    changed = True
            if changed:
                # Bug a06c: mark this EDIT event with source=inbound so
                # the local_label_intent computation skips it when
                # building the "user-intent" tag set. Without the marker,
                # a Jira-side ADD that the inbound differ applies locally
                # would enter local's tag history and then look identical
                # to user intent on the next pass — so a subsequent
                # Jira-side REMOVE would be cancelled by a spurious
                # outbound ADD (T4 IB-REMOVE regression).
                path = _write_event_file(
                    tracker_dir,
                    local_id,
                    "EDIT",
                    {"fields": {"tags": new_tags}, "source": "inbound"},
                )
                written.append(str(path))

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
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_apply_inbound_update: rebar.link failed for %s -> %s (%s): %r",
                    local_id,
                    target_local_id,
                    relation,
                    exc,
                )

    return ApplyResult(
        mutation.direction,
        mutation.action,
        {"local_id": local_id, "events": written, "links_applied": links_applied},
    )


def _apply_inbound_delete(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Handle one of four probe-outcome branches when a Jira issue has
    disappeared from the working set.

    Branches (selected via ``mutation.payload['probe_outcome']``):
      * ``hard_delete``  — preserve the local content + emit a follow-on
        ``(outbound, create_after_hard_delete)`` mutation so reconcile_once
        re-creates the Jira side on the next applier pass.
      * ``redirect``     — rename the local jira-dig-NNN ticket directory to
        the new key supplied under ``new_jira_key``.
      * ``out_of_window``— write a COMMENT event noting the Jira issue is
        closed and aged out of the working set (no local mutation otherwise).
      * ``trash``        — write a COMMENT event noting recoverable trash state.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    branch = payload.get("probe_outcome", "out_of_window")
    target = mutation.target
    local_id = target if target.startswith("jira-") else _jira_key_to_local_id(target)
    tracker_dir = _resolve_tracker_dir(repo_root)
    result_payload: dict[str, Any] = {"branch": branch, "local_id": local_id}

    if branch == "hard_delete":
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {
                "comment": (
                    f"reconciler: Jira issue {target} hard-deleted; local "
                    "content preserved. Outbound re-create follow-on emitted."
                )
            },
        )
        follow_on = {
            "direction": "outbound",
            "action": "create_after_hard_delete",
            "target": target,
            "local_id": local_id,
        }
        result_payload["follow_on"] = follow_on
        # TODO(epic-3e36): wire the follow-on mutation into reconcile_once so
        # the outbound re-create runs in the same pass. Tracked separately.
    elif branch == "redirect":
        new_key = payload.get("new_jira_key", "")
        new_local_id = _jira_key_to_local_id(new_key) if new_key else local_id + "-redirected"
        src = tracker_dir / local_id
        dst = tracker_dir / new_local_id
        # Collision protection (PR #375 review thread 3307104042): when both
        # src and dst already exist on disk (prior failed pass, or the
        # destination key was imported by another path) we cannot silently
        # skip the rename — that leaves the tracker holding two ticket dirs
        # for the same logical ticket. Raise so the operator can reconcile.
        if src.exists() and dst.exists():
            raise FileExistsError(
                f"inbound delete redirect: refusing to rename {src} -> {dst} "
                f"because destination already exists (target={target}, "
                f"new_jira_key={new_key!r})"
            )
        if src.exists() and not dst.exists():
            src.rename(dst)
        # Write a comment noting the redirect on the destination directory.
        _write_event_file(
            tracker_dir,
            new_local_id,
            "COMMENT",
            {"comment": f"reconciler: redirected from {target} -> {new_key}"},
        )
        result_payload["new_local_id"] = new_local_id
    elif branch == "out_of_window":
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {
                "comment": (
                    f"reconciler: Jira issue {target} is closed and has aged "
                    "out of the working window."
                )
            },
        )
    elif branch == "trash":
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {"comment": (f"reconciler: Jira issue {target} entered recoverable trash state.")},
        )
    else:
        # Unknown branch: record observable evidence; do not raise so the pass
        # converges. The structural test enforces real-body coverage.
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {"comment": f"reconciler: unknown probe_outcome={branch!r}"},
        )

    return ApplyResult(mutation.direction, mutation.action, result_payload)


def _apply_inbound_probe(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Inbound probe leaf: probe execution lives in reconcile.route_inbound_probe.

    The leaf itself is a marker — the probe classification and follow-on
    generation happen upstream of applier dispatch. We still write an audit
    comment so the dispatch path is observable in the local tracker when a
    probe leaf is invoked directly.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)
    target = mutation.target
    local_id = target if target.startswith("jira-") else _jira_key_to_local_id(target)
    tracker_dir = _resolve_tracker_dir(repo_root)
    ticket_dir = tracker_dir / local_id
    if ticket_dir.exists():
        _write_event_file(
            tracker_dir,
            local_id,
            "COMMENT",
            {"comment": f"reconciler: inbound probe acknowledged for {target}"},
        )
    return ApplyResult(
        mutation.direction, mutation.action, {"local_id": local_id, "probed": target}
    )


def _apply_inbound_clean_label(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Remove rebar-id-* labels from a Jira issue.

    Inbound-only leaf: invoked when the differ has detected stale or duplicated
    `rebar-id-*` labels on the Jira side that need to be removed. The mutation
    payload carries the labels to remove under ``labels_to_remove``; only labels
    that match the ``rebar-id-*`` pattern are removed (defensive filter against a
    misshapen payload). All client calls go through :func:`_call_with_retry`
    so transient 5xx/429/timeout failures retry with backoff.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)
    if client is None:
        # Stub path: preserved for tests that don't exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})
    labels = mutation.payload.get("labels_to_remove") or []
    removed: list[str] = []
    for label in labels:
        # Defensive: only remove labels matching the rebar-id-* pattern.
        if not isinstance(label, str) or not label.startswith("rebar-id-"):
            continue
        _call_with_retry(client.remove_label, mutation.target, label)
        removed.append(label)
    return ApplyResult(mutation.direction, mutation.action, {"removed": removed})


def _apply_inbound_repair_property(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Repair a missing ``local_id`` entity property on a Jira issue.

    Delegates to the existing :func:`inbound_repair_property` implementation
    (kept under its legacy name for back-compat with existing tests). Wraps
    the outcome dict into an ``ApplyResult`` so it routes cleanly through the
    typed-mutation dispatch table.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)
    if client is None:
        # Stub path: preserved for tests that don't exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})
    outcome = inbound_repair_property(mutation, client)
    return ApplyResult(mutation.direction, mutation.action, outcome)


def _apply_inbound_conflict(mutation, *, client=None, repo_root=None) -> ApplyResult:
    """Emit a ``suppress_pair`` follow-on and a ``pending_bug_ticket`` directive
    for an unresolved (local, Jira) conflict.

    Bug filing is DEFERRED out of the apply loop because the
    ``rebar create bug`` CLI commits to the ``tickets``
    orphan branch, which would advance HEAD inside ``_apply_batch``'s
    drift-guarded loop and raise a spurious ``HeadDriftError`` (bug d822).
    The caller (``apply``) collects pending_bug_ticket directives during the
    inbound dispatch loop and files them after ``_apply_batch`` returns —
    outside the drift guard's scope.

    The follow_on still emits during the leaf so reconcile_once can drop
    subsequent mutations for the same pair in the same pass; suppression
    semantics are unchanged.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    jira_key = mutation.target
    local_id = payload.get("local_id", "")
    reason = payload.get("reason", "unspecified")
    parent_id = payload.get("parent_id") or _rebar_env("RECONCILER_CONFLICT_PARENT_ID", "")
    title = f"[Reconciler conflict]: pair ({local_id!r}, {jira_key!r}) -> {reason}"
    description = (
        f"Reconciler detected a conflict on (local_id={local_id!r}, "
        f"jira_key={jira_key!r}).\n\n"
        f"Reason: {reason}\n\n"
        "## Expected Behavior\n"
        "Conflict is resolved or suppressed before the next reconciler pass.\n\n"
        "## Actual Behavior\n"
        f"Conflict surfaced during applier dispatch with reason={reason!r}."
    )

    follow_on = {
        "kind": "suppress_pair",
        "local_id": local_id,
        "jira_key": jira_key,
    }
    pending_bug_ticket = {
        "title": title,
        "description": description,
        "parent_id": parent_id,
        "local_id": local_id,
        "jira_key": jira_key,
    }
    return ApplyResult(
        mutation.direction,
        mutation.action,
        {
            "follow_on": follow_on,
            "pending_bug_ticket": pending_bug_ticket,
        },
    )


def inbound_repair_property(mutation, client) -> dict:
    """Repair a missing entity property on a Jira issue.

    Happy path: invokes ``client.set_issue_property(target, 'local_id', local_id)``
    and returns ``{'status': 'ok', 'key': target}``.

    Failure path: when ``set_issue_property`` raises, attempts a follow-on cleanup
    via ``client.remove_label(target, 'rebar-id-<local_id>')`` (best-effort — a
    ``remove_label`` exception is captured, NOT raised), and returns an outcome
    dict with ``status='repair_property_failed'`` plus a top-level ``follow_on``
    payload whose ``kind`` is ``'schema_drift_signal'``. The follow-on is the
    signalling seam consumed by reconcile.py; this function MUST NOT import
    invariants directly — preserving the invariants-as-upstream-phase contract
    (see ticket 44e6-4916 AC: "applier.py does NOT import invariants").

    The follow_on field sits at the TOP LEVEL of the outcome dict (not nested
    under 'result'); manifest canonical-form serialization is expected to
    EXCLUDE follow_on fields when computing the content-addressable hash
    (per AC amendment G2 on ticket 44e6-4916).

    Args:
        mutation: Object exposing ``.target`` (Jira issue key) and ``.payload``
                  (mapping with at least a ``'local_id'`` entry).
        client:   AcliClient (or compatible test double) exposing
                  ``set_issue_property`` and ``remove_label``.

    Returns:
        Outcome dict — see status taxonomy above.
    """
    target = mutation.target
    payload = mutation.payload or {}
    local_id = payload.get("local_id", "")

    try:
        client.set_issue_property(target, "local_id", local_id)
        return {"status": "ok", "key": target, "follow_on": None}
    except Exception as exc:
        label_remove_err: Exception | None = None
        try:
            client.remove_label(target, f"rebar-id-{local_id}")
        except Exception as e:
            label_remove_err = e

        return {
            "status": "repair_property_failed",
            "key": target,
            "follow_on": {
                "kind": "schema_drift_signal",
                "issue_key": target,
                "reason": f"repair_property_failed: {exc}",
                "label_remove_error": (
                    str(label_remove_err) if label_remove_err is not None else None
                ),
            },
        }
