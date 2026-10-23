"""Build outbound create and update mutations for the differ.

Pass-wide inputs live in the existing ``OutboundDiffConfig``; backend ports
remain explicit parameters. The split preserves the differ's behavior and
avoids a second generic context object (ADR 0107).
"""

from __future__ import annotations

import sys

from rebar.reconciler.outbound_comments import _diff_comments, _map_comments_for_create
from rebar.reconciler.outbound_differ import (
    _BRIDGE_TARGET_PROJECT_KEY,
    _DELETED,
    _TRANSPORT_ERROR,
    OutboundDiffConfig,
    OutboundMutation,
    _best_effort,
    _effective_create_defaults_for,
    _effective_link_map_for,
    _effective_priority_map_for,
    _effective_status_map_for,
    _effective_type_map_for,
    _is_retired,
    _safe_get_issue,
)
from rebar.reconciler.outbound_field_diff import compute_update_fields
from rebar.reconciler.outbound_labels import (
    _EXCLUDED_PREFIXES,
    _diff_labels,
    _diff_status_annotation_labels,
    _diff_type_annotation_labels,
)
from rebar.reconciler.outbound_links import _diff_links


def _create_prerequisites(ticket, binding_store) -> tuple[str, ...]:
    """Depend on an unbound parent's create by local ID, without inventing a Jira key."""
    parent_id = ticket.get("parent_id")
    if not isinstance(parent_id, str) or not parent_id:
        return ()
    if _best_effort(binding_store, "get_jira_key", parent_id):
        return ()
    return (parent_id,)


def _compute_outbound_create_mutation(
    mutations,
    ticket,
    status,
    local_id,
    config: OutboundDiffConfig,
    binding_store,
    outbound_mapper,
) -> None:
    """Append a create for an unbound ticket unless a retired binding forbids it.

    A confirmed tombstone must not resurrect as a new issue; never-bound
    tickets still create. The injected mapper records a suppressed non-epic
    parent in the existing dropped-field sink, keyed by local ID because no
    Jira key exists yet. A create cannot use update's remote convergence gate.
    """
    local_ticket_types = config.local_ticket_types
    mapping = config.projects_mapping
    repo_root = config.repo_root
    effective_cache = config.effective_cache
    dropped_field_sink = config.dropped_field_sink

    tombstone = _best_effort(binding_store, "retired_key_for_local", local_id)
    if isinstance(tombstone, str) and tombstone:
        _best_effort(binding_store, "note_create_suppressed", local_id, tombstone)
        return
    for remote_key, remote_fields in (config.jira_snapshot or {}).items():
        if isinstance(remote_fields, dict) and str(remote_fields.get("local_id") or "") == local_id:
            _best_effort(binding_store, "bind_confirm", local_id, remote_key)
            return
    # Unbound -> outbound create
    # ticket 929a: for new issues the Jira side has no labels yet,
    # so the annotation label only needs an ADD (never a REMOVE).
    status_map = _effective_status_map_for(ticket, mapping, repo_root, cache=effective_cache)
    type_map = _effective_type_map_for(ticket, mapping, repo_root, cache=effective_cache)
    priority_map = _effective_priority_map_for(ticket, mapping, repo_root, cache=effective_cache)
    create_defaults = _effective_create_defaults_for(
        ticket, mapping, repo_root, cache=effective_cache
    )
    # S3: the type-axis annotation mirror — a collapsing per-project type_map stamps a
    # rebar-type: label so the local type is recoverable inbound. New issues carry no
    # labels yet, so only an ADD is possible here (never a REMOVE), like status.
    annotation_mutations = _diff_status_annotation_labels(
        local_status=status,
        jira_labels=[],
        status_map=status_map,
    ) + _diff_type_annotation_labels(
        ticket.get("ticket_type", "task"),
        [],
        type_map=type_map,
    )
    suppressed_parents: list[str] = []
    create_fields = outbound_mapper.map_local_to_remote(
        ticket,
        binding_store=binding_store,
        local_ticket_types=local_ticket_types,
        suppressed_out=suppressed_parents,
        status_map=status_map,
        type_map=type_map,
        priority_map=priority_map,
        create_defaults=create_defaults,
    )
    if suppressed_parents and dropped_field_sink is not None:
        dropped_field_sink.append((local_id, "parent"))
    # With a project mapping, stamp each create's resolved target. Reject
    # unmapped projects here because creates bypass the applier's project guard;
    # an unseeded single-project store retains the transport default.
    if mapping is not None and getattr(mapping, "projects", None):
        from rebar.reconciler import projects_store

        target = projects_store.resolve_project(ticket, mapping)
        if not target:
            return
        # Match the applier's case-insensitive guard but stamp the canonical
        # mapping key. A stale key suppresses the create.
        canonical = next((k for k in mapping.projects if k.upper() == target.upper()), None)
        if canonical is None:
            return
        create_fields[_BRIDGE_TARGET_PROJECT_KEY] = canonical
    requires_create = _create_prerequisites(ticket, binding_store)
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
            requires_create=requires_create,
        )
    )


def _compute_outbound_update_mutation(
    mutations,
    ticket,
    status,
    local_id,
    jira_key,
    config: OutboundDiffConfig,
    binding_store,
    outbound_mapper,
    inbound_mapper,
    assignee_resolver,
) -> None:
    """Append an update for a bound ticket only when a resolved field diverges.

    Bound keys absent from search use a bounded direct GET; unresolved keys
    emit nothing. ``config`` holds pass-wide values while backend collaborators
    stay explicit.
    """
    jira_snapshot = config.jira_snapshot
    # Both are populated by compute_outbound_mutations before the per-ticket loop.
    assert jira_snapshot is not None
    client = config.client
    pass_id = config.pass_id
    _selected_for_get_this_pass = config.selected_for_get
    prev_snapshot = config.prev_snapshot
    local_label_intent = config.local_label_intent
    local_ticket_types = config.local_ticket_types
    _assignee_resolver = assignee_resolver
    links = config.links
    local_parents = config.local_parents
    mapping = config.projects_mapping
    repo_root = config.repo_root
    effective_cache = config.effective_cache
    absent_alive_fields = config.absent_alive_fields
    assert absent_alive_fields is not None
    conflict_sink = config.conflict_sink
    dropped_field_sink = config.dropped_field_sink

    # Search absence is not an empty issue: GET to distinguish deleted from
    # alive out-of-window keys before computing a field diff.
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
            # A 404 may mean deletion or a project move. Recheck by immutable
            # ID; only unresolved absence advances retirement grace.
            _best_effort(binding_store, "note_absent_or_rekey", jira_key, client)
            return
        if fields is _TRANSPORT_ERROR:
            # Non-404 HTTPError / URLError / timeout — transient.
            # Emit nothing, warn, defer; counter untouched.
            print(  # noqa: T201
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
        # Share only alive fields with inbound diffing; deleted and failed GETs
        # stay outbound-owned and are never mirrored back.
        absent_alive_fields[jira_key] = fields

    # Ticket 625b: the whole vendor-neutral field path (canonicalize snapshot +
    # baseline, diff in local shape, map back to vendor shape) lives in the core helper.
    _status_map = _effective_status_map_for(ticket, mapping, repo_root, cache=effective_cache)
    _type_map = _effective_type_map_for(ticket, mapping, repo_root, cache=effective_cache)
    _priority_map = _effective_priority_map_for(ticket, mapping, repo_root, cache=effective_cache)
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
        priority_map=_priority_map,
    )
    # Comments reuse the resolved snapshot and persistent comment IDs. The
    # backend codec normalizes local dedup keys like the send path.
    comment_mutations = _diff_comments(
        ticket,
        jira_key,
        comment_snapshot,
        client=client,
        inbound_mapper=inbound_mapper,
        binding_store=binding_store,
        codec=getattr(outbound_mapper, "comment_codec", None),
        local_parents=local_parents,
        local_ticket_types=local_ticket_types,
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
    # S3: type-axis annotation mutations (rebar-type:<local>) — ADD the desired label
    # for a collapsing per-project type_map and REMOVE any stale rebar-type: label, in
    # lock-step with the status annotation above.
    annotation_mutations = annotation_mutations + _diff_type_annotation_labels(
        ticket.get("ticket_type", "task"),
        list(jira_fields.get("labels") or []),
        type_map=_type_map,
    )
    label_mutations = label_mutations + annotation_mutations
    # story 25ae Cycle 2: diff local deps -> Jira issuelinks (ADD-only,
    # deduped against the snapshot's existing issuelinks so an
    # already-present link emits nothing — no per-pass churn).
    link_mutations = _diff_links(
        ticket,
        jira_fields,
        binding_store,
        links,
        link_map=_effective_link_map_for(ticket, mapping, repo_root, cache=effective_cache),
    )

    if fields or comment_mutations or label_mutations or link_mutations:
        # Log changed field names, never values, to expose non-converging
        # updates without leaking content. Non-field updates skip the list.
        print(  # noqa: T201
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
