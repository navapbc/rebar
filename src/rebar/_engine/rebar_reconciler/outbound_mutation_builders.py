"""Outbound CREATE/UPDATE mutation builders extracted from outbound_differ.py.

Holds the two per-ticket mutation builders (``_compute_outbound_create_mutation``
and ``_compute_outbound_update_mutation``) that ``compute_outbound_mutations``
orchestrates. Split out of ``outbound_differ.py`` purely for module size; the
behaviour is unchanged.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from rebar_reconciler.outbound_comments import _diff_comments, _map_comments_for_create
from rebar_reconciler.outbound_differ import (
    _BRIDGE_TARGET_PROJECT_KEY,
    _DELETED,
    _TRANSPORT_ERROR,
    OutboundMutation,
    _best_effort,
    _effective_status_map_for,
    _is_retired,
    _safe_get_issue,
)
from rebar_reconciler.outbound_field_diff import compute_update_fields
from rebar_reconciler.outbound_labels import (
    _EXCLUDED_PREFIXES,
    _diff_labels,
    _diff_status_annotation_labels,
)
from rebar_reconciler.outbound_links import _diff_links

if TYPE_CHECKING:
    from ._backend import TicketTransport


def _compute_outbound_create_mutation(
    mutations,
    ticket,
    status,
    local_id,
    binding_store,
    local_ticket_types,
    outbound_mapper,
    *,
    dropped_field_sink: list[tuple[str, str]] | None = None,
    mapping: Any = None,
    repo_root: Any = None,
) -> None:
    """Phase: append the outbound CREATE mutation for an unbound local ticket.

    ``outbound_mapper`` is the injected Backend-port ``OutboundMapper`` (ticket 4af8);
    its ``map_local_to_remote`` replaces the former direct vendor-mapper import.

    DROPPED-PARENT REPORTING (ticket 8390): bug 8b25's hierarchy guard omits a non-epic
    parent from the mapped fields, and on this path that omission was totally silent —
    a ticket created under a non-epic parent lost its hierarchy at birth with no durable
    trace. Report it on the EXISTING drop channel (``run_differs._emit_outbound_field_alerts``
    turns the pair into a deduped ``outbound-field-dropped`` bridge alert), keyed by the
    LOCAL id because a create has no Jira key yet by construction.

    Unlike the sibling UPDATE path there is no convergence gate here, and there must not
    be one: at CREATE the issue does not exist on the tracker, so there is no remote
    parent it could already match — the drop is unconditionally a real loss.

    TOMBSTONE GUARD (bug 3b5f): a local ticket with NO live binding but WITH a retired
    one was paired with a Jira issue a bounded direct GET confirmed 404 — deleted.
    Retirement unbinds the local ticket, so without this check the ordinary
    unbound->create arm resurrected the deliberately-deleted issue ~3 passes later.
    A NEVER-bound ticket has no tombstone and still creates; that distinction is the
    whole point. Fail-open via ``_best_effort``, and reversible by ``unretire``.
    """
    tombstone = _best_effort(binding_store, "retired_key_for_local", local_id)
    if isinstance(tombstone, str) and tombstone:
        _best_effort(binding_store, "note_create_suppressed", local_id, tombstone)
        return
    # Unbound -> outbound create
    # ticket 929a: for new issues the Jira side has no labels yet,
    # so the annotation label only needs an ADD (never a REMOVE).
    status_map = _effective_status_map_for(ticket, mapping, repo_root)
    annotation_mutations = _diff_status_annotation_labels(
        local_status=status,
        jira_labels=[],
        status_map=status_map,
    )
    suppressed_parents: list[str] = []
    create_fields = outbound_mapper.map_local_to_remote(
        ticket,
        binding_store=binding_store,
        local_ticket_types=local_ticket_types,
        suppressed_out=suppressed_parents,
        status_map=status_map,
    )
    if suppressed_parents and dropped_field_sink is not None:
        dropped_field_sink.append((local_id, "parent"))
    # Story d19d: resolve the target project per ticket and stamp it, so BOTH
    # transports write to the ticket's project rather than one construction-time
    # default. Gated on a non-empty mapping so an unseeded (single-project) store
    # keeps its legacy behaviour (create, no stamp — the transport's own project
    # applies). A ticket that resolves to "not synced" (None), or names a project
    # NOT in the mapping (a stale/typo binding), emits NO create: creates are
    # exempt from the applier's cross-project guard, so this is the only gate that
    # can stop a create against an unsynced project.
    if mapping is not None and getattr(mapping, "projects", None):
        from rebar_reconciler import projects_store

        target = projects_store.resolve_project(ticket, mapping)
        if not target:
            return
        # Bug 7b9a finding 1: match CASE-INSENSITIVELY so this create-path check
        # agrees with the applier's cross-project guard, which uppercases both
        # sides (applier._cross_project_targets). Stamp the CANONICAL mapping key
        # (not the ticket's raw case) so the transport routes to the real project.
        # A key with no case-folded match (stale/typo binding) still emits no
        # create — creates are guard-exempt, so this is the only gate that stops
        # a create against an unsynced project.
        # Finding 4 (accepted): the mapping is also read by the applier guard
        # (read_projects) later in the same pass. That two-read window is left as
        # is — projects.json is a rare operator CLI write and a pass is a short
        # single window, so a mid-pass divergence is not worth threading one read.
        canonical = next((k for k in mapping.projects if k.upper() == target.upper()), None)
        if canonical is None:
            return
        create_fields[_BRIDGE_TARGET_PROJECT_KEY] = canonical
    mutations.append(
        OutboundMutation(
            local_id=local_id,
            jira_key=None,
            action="create",
            fields=create_fields,
            comments=_map_comments_for_create(ticket),
            labels=(
                [
                    {"action": "add", "label": t}
                    for t in sorted(ticket.get("tags", []))
                    if not any(t.startswith(p) for p in _EXCLUDED_PREFIXES)
                ]
                + annotation_mutations
            ),
            links=[],  # links resolved after all creates
        )
    )


def _compute_outbound_update_mutation(
    mutations,
    ticket,
    status,
    local_id,
    jira_key,
    jira_snapshot,
    binding_store,
    client: TicketTransport,
    pass_id,
    _selected_for_get_this_pass,
    prev_snapshot,
    local_label_intent,
    local_ticket_types,
    _assignee_resolver,
    absent_alive_fields,
    outbound_mapper,
    inbound_mapper,
    links,
    *,
    conflict_sink: list[tuple[str, str]] | None = None,
    dropped_field_sink: list[tuple[str, str]] | None = None,
    mapping: Any = None,
    repo_root: Any = None,
) -> None:
    """Phase: for a bound ticket, resolve jira_fields (including the bounded
    bound-but-absent direct GET) and append an outbound UPDATE mutation when anything
    diverged. A bare ``return`` skips the ticket (emit nothing)."""
    # Bound -> compare fields, emit update if different.
    #
    # Bug 1e08-1a35-0267-4ca6: discriminate on MEMBERSHIP, not value.
    # A bound key ABSENT from this pass's search snapshot must NOT diff
    # against ``{}`` (that re-emits every field every pass). Two absence
    # sub-classes: (a) deleted → direct GET 404; (b) status=Done beyond
    # _DONE_RECENT_CAP → alive (HTTP 200) but absent from the search
    # snapshot. We resolve the real fields via a bounded direct GET.
    if jira_key in jira_snapshot:
        # EXISTING path — key present in the search snapshot.
        jira_fields = jira_snapshot[jira_key]
        comment_snapshot = jira_snapshot
    else:
        # Bound-but-absent from THIS pass's working set.
        if client is None:
            # No client → we cannot direct-GET to resolve the absence.
            # Skip (defer) rather than diff against {} — that re-emit
            # against an empty dict was the original defect (bug 1e08).
            # Mirrors the _diff_comments no-client safety pattern.
            return
        if _is_retired(binding_store, jira_key):
            return  # known-dead; no GET, no emit (budget preserved)
        if jira_key not in _selected_for_get_this_pass:
            return  # not selected this pass → DEFERRED (no emit)

        fields = _safe_get_issue(client, jira_key)
        # Record the GET regardless of outcome (rotation bookkeeping).
        _best_effort(binding_store, "set_last_get", jira_key, pass_id)

        if fields is _DELETED:
            # HTTPError 404 — gone, OR MOVED to another project and re-keyed
            # (bug 7c26). The store re-asks by immutable numeric id and re-keys
            # on a hit; only an unproven absence bumps the consecutive-404
            # counter (may retire at GRACE). Emit nothing either way.
            _best_effort(binding_store, "note_absent_or_rekey", jira_key, client)
            return
        if fields is _TRANSPORT_ERROR:
            # Non-404 HTTPError / URLError / timeout — transient.
            # Emit nothing, warn, defer; counter untouched.
            print(
                f"WARNING: outbound_differ: direct GET for bound-but-absent "
                f"{jira_key!r} failed (transport error). Deferring this "
                f"key's sync to a later pass (no mutation emitted).",
                file=sys.stderr,
            )
            return

        # HTTP 200 — issue is alive (out-of-window). Reset the absence
        # counter and build a one-key overlay so the SAME diff path runs.
        _best_effort(binding_store, "clear_absent", jira_key)
        jira_fields = fields
        comment_snapshot = dict(jira_snapshot)
        comment_snapshot[jira_key] = fields
        # Bug 0702: share this alive GET result with the inbound differ
        # so the out-of-window key is mirrored Jira→local without a
        # second GET. Only the alive (200) case is recorded — 404 and
        # transport errors are intentionally left out so a gone issue is
        # never inbound-mirrored (retirement stays outbound-owned).
        absent_alive_fields[jira_key] = fields

    # Ticket 625b: the whole vendor-neutral field path (canonicalize snapshot +
    # baseline, diff in local shape, map back to vendor shape) lives in the core helper.
    _status_map = _effective_status_map_for(ticket, mapping, repo_root)
    fields = compute_update_fields(
        ticket,
        jira_fields,
        inbound_mapper=inbound_mapper,
        outbound_mapper=outbound_mapper,
        binding_store=binding_store,
        local_id=local_id,
        jira_key=jira_key,
        local_ticket_types=local_ticket_types,
        assignee_resolver=_assignee_resolver,
        prev_snapshot=prev_snapshot,
        conflict_sink=conflict_sink,
        dropped_field_sink=dropped_field_sink,
        status_map=_status_map,
    )
    # Comments use the resolved snapshot (the bounded-GET overlay) — NO second call (C3).
    # emersed-specific-mutt: thread the binding_store so _diff_comments' PRIMARY skip can
    # consult the persistent comment_ids map, and the backend's comment codec so the LOCAL
    # dedup key is normalized through the SAME RichTextCodec the send path renders with
    # (injected via the Backend port — the shared layer never imports a concrete codec).
    comment_mutations = _diff_comments(
        ticket,
        jira_key,
        comment_snapshot,
        client=client,
        inbound_mapper=inbound_mapper,
        binding_store=binding_store,
        codec=getattr(outbound_mapper, "comment_codec", None),
    )
    # bug a06c: intent-gated REMOVE. When local_label_intent is
    # provided but lacks an entry for this local_id, fall back to
    # an empty intent set (lazy first-pass safety: suppresses all
    # REMOVEs for tickets we have no event-log evidence for).
    intent_set: set[str] | None = None
    if local_label_intent is not None:
        intent_set = local_label_intent.get(local_id, set())
    label_mutations = _diff_labels(ticket, jira_fields, intent_set)
    # ticket 929a: status annotation labels (rebar-status:blocked/cancelled)
    # are managed separately from user tags (excluded from _diff_labels via
    # _EXCLUDED_PREFIXES). Compute and merge annotation mutations here.
    annotation_mutations = _diff_status_annotation_labels(
        local_status=status,
        jira_labels=list(jira_fields.get("labels") or []),
        status_map=_status_map,
    )
    label_mutations = label_mutations + annotation_mutations
    # story 25ae Cycle 2: diff local deps -> Jira issuelinks (ADD-only,
    # deduped against the snapshot's existing issuelinks so an
    # already-present link emits nothing — no per-pass churn).
    link_mutations = _diff_links(ticket, jira_fields, binding_store, links)

    if fields or comment_mutations or label_mutations or link_mutations:
        # Sync-hardening P5 / bug 57d1: emit a one-line CHANGED-FIELD BREADCRUMB
        # (field NAMES only, never values — descriptions/assignees may be large or
        # sensitive) whenever a bound key gets an outbound UPDATE carrying field diffs,
        # so a re-emitting (non-converging) field is visible in CI logs. Comment-/label-
        # only updates carry no field diff, so the breadcrumb is skipped.
        print(
            f"RECON: outbound_update key={jira_key} "
            f"changed=[{','.join(sorted(fields))}] "
            f"comments={len(comment_mutations)} "
            f"labels={len(label_mutations)} "
            f"links={len(link_mutations)}",
            file=sys.stderr,
        )
        mutations.append(
            OutboundMutation(
                local_id=local_id,
                jira_key=jira_key,
                action="update",
                fields=fields,
                comments=comment_mutations,
                labels=label_mutations,
                links=link_mutations,
            )
        )
