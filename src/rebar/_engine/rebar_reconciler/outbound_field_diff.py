"""Canonical (local-shape) outbound field diff for bidirectional sync (ticket 625b).

The outbound UPDATE path used to compare local ticket state against the RAW Jira
snapshot shape (``outbound_fields._diff_fields``). This module re-homes that
comparison into the vendor-neutral core: it diffs the LOCAL ticket against a
snapshot that has ALREADY been canonicalized to local shape by the injected
``InboundMapper`` (mirroring the inbound differ), producing a canonical
``changed`` dict keyed by LOCAL field names. The caller then maps that back to the
backend's field shapes at the emission boundary via
``OutboundMapper.map_fields_to_remote``.

Consequences of the seam:

* This module imports NOTHING from ``adapters.jira`` and names no raw Jira
  snapshot key — vendor shapes cross the core only as opaque payloads produced
  and consumed at the mapper port calls.
* Every decision the old vendor-shape differ made is preserved: local-wins,
  the issuetype/ticket_type update exclusion, inbound-directionality
  suppression, the assignee identity/resolver fast-path (incl. the
  ``_assignee_is_account_id`` sentinel), the managed-parent-clear gate, the
  description parity fit, the reporter one-way diff, and the
  conflict/dropped-field observability sinks.

The description ADF fit and the assignee account resolution are the two
vendor-specific operations still needed; both are reached ONLY through the
injected ``OutboundMapper`` (``map_fields_to_remote`` fits the description;
``resolve_assignee`` runs the account search), so this module stays pure/neutral.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rebar_reconciler._backend import OutboundMapper

# Fields the INBOUND differ mirrors Jira→local. A Jira-side change to one of
# these, when local is unchanged since the last sync (matches the baseline),
# flows inbound rather than being reverted by local-wins. This is the arbitrated
# set named by ADR 0026 ("the five inbound-mirrored scalar fields") — title
# included: the pre-625b differ iterated Jira-shaped keys (whose title key is
# ``summary``) and so never suppressed title, which silently reverted a remote
# summary edit over an unmodified local title. See
# ``docs/adr/0026-reconciler-three-way-merge-baseline.md``.
_INBOUND_MIRRORED_FIELDS = frozenset({"title", "description", "priority", "status", "assignee"})


def _text_matches(a: Any, b: Any) -> bool:
    """String comparison tolerant of trailing whitespace (Jira strips it on write),
    falling back to plain equality for non-strings."""
    if isinstance(a, str) and isinstance(b, str):
        return a.rstrip() == b.rstrip()
    return a == b


def _assignee_candidates(scalar: Any, identity: dict[str, Any] | None) -> set[str]:
    """The set of remote identity forms a local assignee may equal: the scalar
    ``assignee`` (a bare display/username string, or the extracted displayName of a
    dict) plus every non-None value of ``assignee_identity`` (display/email/account_id)."""
    candidates: set[str] = set()
    if scalar is not None and str(scalar).strip():
        candidates.add(str(scalar).strip())
    if identity:
        for v in identity.values():
            if v is not None and str(v).strip():
                candidates.add(str(v).strip())
    return candidates


def _assignee_matches(local_val: str, scalar: Any, identity: dict[str, Any] | None) -> bool:
    """Shape-tolerant assignee equality against a canonical remote assignee.

    ``local_val`` matches when it equals ANY remote identity form (scalar string or a
    non-None identity value); both-empty (no candidates, empty local) also matches.
    Mirrors the pre-625b ``_assignee_matches`` against the raw Jira value (dict OR
    bare string)."""
    candidates = _assignee_candidates(scalar, identity)
    if not candidates:
        return (local_val or "") == ""
    return (local_val or "").strip() in candidates


def _resolve_reporter_account_id(local_reporter: Any) -> str | None:
    """Resolve a local reporter string (identity id / email) to a remote accountId via
    rebar core's identity seam, or ``None`` on any miss (best-effort, never raises)."""
    if not local_reporter or not isinstance(local_reporter, str):
        return None
    try:
        from rebar._commands import identity as _identity

        return _identity.jira_account_id(local_reporter)
    except Exception:  # noqa: BLE001 — best-effort; an unresolvable reporter is a miss
        return None


def _diff_reporter(
    ticket: dict[str, Any], reporter_identity: dict[str, Any] | None, changed: dict[str, Any]
) -> None:
    """One-way reporter diff (264f): emit the RAW local ``reporter`` string into
    ``changed`` when it diverges from the remote reporter's accountId. Reporter is an
    UPDATE-only sub-call; the dispatch layer re-resolves the raw string."""
    local_reporter = ticket.get("reporter") or None
    if not local_reporter:
        return
    remote_acct = reporter_identity.get("account_id") if reporter_identity else None
    desired = _resolve_reporter_account_id(local_reporter)
    if desired is not None and desired == (remote_acct or None):
        return  # already the correct reporter — no churn
    if desired is None and remote_acct is None:
        return  # unresolvable reporter and remote has none — nothing to do
    changed["reporter"] = local_reporter


def _resolve_local_parent(
    ticket: dict[str, Any],
    binding_store: Any,
    local_ticket_types: dict[str, str] | None,
    *,
    suppressed_out: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Resolve the local parent to a remote key for the UPDATE diff (present?, value).

    Mirrors ``_map_local_to_jira_fields``' parent logic exactly (ticket 8b25 +
    the symmetric parent-detach clear), but is pure binding-store logic — no vendor
    dependency. Returns ``(present, value)``:

    * a bound, epic (or type-unknown) parent → ``(True, <remote key>)``;
    * a non-epic parent, or an unbound parent → ``(False, None)`` (omitted, retry);
    * a locally-DETACHED ticket (no parent_id) → ``(True, None)`` — the clear
      candidate the diff loop gates on the managed-ref check;
    * no binding store → ``(False, None)``.

    The two ``(False, None)`` returns are byte-identical but mean OPPOSITE things, and
    conflating them is what made a suppressed hierarchy edit invisible: an UNBOUND parent
    self-resolves on a later pass, while a BOUND non-epic parent will NEVER converge — the
    user's parent edit is dropped for good, yet the pass exits 0 reporting convergence
    (ticket 9f26; the same class of silence ``pass_io.record_parent_divergence`` was built
    for).

    ``suppressed_out`` is how the caller learns which of the two happened: the BOUND
    non-epic case appends the remote key it WOULD have emitted. This reports the fact and
    stops there deliberately — whether that suppression actually costs the user anything
    depends on what the tracker already holds, and this function cannot see the remote
    snapshot. ``diff_canonical_fields`` owns that comparison, because a parent the tracker
    ALREADY carries was not dropped at all (the common shape: an inbound-mirrored sub-task's
    parent is a non-epic and is already correct), and alerting there would fire every pass
    for every such ticket and drain the channel of meaning.

    The suppression itself is unchanged: the parent is still omitted from ``changed``, and
    the unbound case still appends nothing.
    """
    if binding_store is None:
        return (False, None)
    local_parent_id = ticket.get("parent_id") or None
    if local_parent_id:
        if local_ticket_types is not None and local_parent_id in local_ticket_types:
            parent_type = (local_ticket_types.get(local_parent_id) or "").lower()
            if parent_type != "epic":
                # Jira permits only Epic parents — suppress (8b25). Report the suppression
                # (bound parents only; an unbound one has not been offered to the tracker
                # yet and converges later) and let the caller judge whether it cost anything.
                if suppressed_out is not None:
                    suppressed_key = binding_store.get_jira_key(local_parent_id)
                    if suppressed_key:
                        suppressed_out.append(suppressed_key)
                return (False, None)
        remote_parent_key = binding_store.get_jira_key(local_parent_id)
        if remote_parent_key:
            return (True, remote_parent_key)
        return (False, None)  # unbound this pass — omit, retry next pass
    # Detached locally: emit an explicit clear candidate (compared against remote).
    return (True, None)


def _parent_clear_is_managed(
    remote_parent_key: str, ticket: dict[str, Any], binding_store: Any
) -> bool:
    """Whether a detached-locally remote parent is one we MANAGED (so its CLEAR may
    propagate). Fail-open toward NOT clobbering a human-set parent (adopt inbound)."""
    from rebar.reducer._managed_refs import should_propagate_removal

    get_local_id = getattr(binding_store, "get_local_id", None)
    if get_local_id is None:
        return False
    parent_local_id = get_local_id(remote_parent_key)
    if not parent_local_id:
        return False
    return should_propagate_removal("parent", parent_local_id, ticket)


def _peer_removed_the_parent(
    local_parent: str | None,
    remote_parent: Any,
    ticket: dict[str, Any],
    binding_store: Any,
    local_id: str,
) -> bool:
    """Whether the peer DE-PARENTED this child since our last observation, so the outbound
    local-wins SET must stand down and let ``diff_inbound_parent`` clear the local parent.

    Ticket 339a-57ac-e5f3-4718: a parent cleared on the Jira side was never cleared locally.
    ``parent`` is not in ``_INBOUND_MIRRORED_FIELDS``, so nothing deferred to inbound for it —
    outbound resolved the still-bound local epic parent, saw ``remote_parent_id is None``, and
    emitted a local-wins re-push; the inbound differ then dropped its own freshly computed
    clear as a same-pass contradiction of that write. The clear's precondition ("Jira has no
    parent, local does") is IDENTICAL to the re-push's, so the clear could never fire.

    The discriminator is the LAST-OBSERVED PEER PARENT (ticket 88d9's evidence channel, reused
    here rather than inventing a second one). A bare "remote is falsy" check cannot tell the
    two symmetric cases apart:

    * ``get_peer_parent(local_id) == local_parent`` — we last saw the peer carrying exactly the
      parent we still hold locally, and it is gone now. Only the PEER moved: a remote removal,
      which inbound owns. Suppress the outbound push (return ``True``).
    * ``get_peer_parent(local_id) != local_parent`` — the local side re-set (or first set) the
      parent since that observation, so this is genuine LOCAL intent and local-wins must push
      it. Returning ``True`` here would silently discard a real local re-parent, a worse
      failure than the one being fixed.

    ``get_peer_parent`` is getattr-guarded exactly as the inbound differ guards it: legacy
    binding stores and older test doubles predate the method, and their absence must fall
    through to today's unconditional local-wins push rather than change behaviour blindly.
    """
    if not local_parent or remote_parent:
        return False
    get_peer_parent = getattr(binding_store, "get_peer_parent", None)
    if get_peer_parent is None:
        return False
    observed = get_peer_parent(local_id or ticket.get("ticket_id") or "")
    return bool(observed) and observed == local_parent


def compute_update_fields(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    *,
    inbound_mapper: Any,
    outbound_mapper: OutboundMapper,
    binding_store: Any = None,
    local_id: str = "",
    jira_key: str = "",
    local_ticket_types: dict[str, str] | None = None,
    assignee_resolver: Any = None,
    prev_snapshot: dict[str, Any] | None = None,
    conflict_sink: list[tuple[str, str]] | None = None,
    dropped_field_sink: list[tuple[str, str]] | None = None,
    status_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Canonicalize the snapshot entry (and arbitration baseline) via the injected
    ``InboundMapper``, diff in LOCAL shape, and map the changed subset back to the
    backend's field shapes via the ``OutboundMapper`` — the whole vendor-neutral field
    path for one bound ticket. Returns the vendor-shaped ``OutboundMutation.fields``.

    Storage stays vendor-shaped: the baseline is mapped at READ time only. The
    client-backed account resolver is bound to this remote key here and passed DOWN as an
    argument (ticket 65d7) so ``resolve_assignee`` can consult the live account search —
    a declared collaborator on the port, not an attribute set on the mapper behind its back.
    """
    canonical_remote = inbound_mapper.map_remote_to_local(jira_fields)
    # Arbitration ancestor (story d6bd): the per-binding baseline when available,
    # falling back to the prev-snapshot entry only for fixture paths that pass neither
    # binding_store nor local_id. The baseline is a raw vendor subset → canonicalize it.
    if binding_store is not None and local_id:
        raw_baseline = binding_store.get_baseline(local_id)
        emit_baseline_cold_start(binding_store, local_id, raw_baseline)
    else:
        raw_baseline = (prev_snapshot or {}).get(jira_key)
    canonical_baseline = inbound_mapper.map_remote_to_local(raw_baseline) if raw_baseline else None
    # Bind the caller's (local_value, jira_key) resolver to THIS remote key and hand it to
    # the diff as an argument (ticket 65d7). It used to be assigned onto the mapper as
    # a private attribute ON the mapper, under a bare ``except ...: pass``, so a mapper
    # that refused the assignment silently lost its live account search and degraded every
    # resolution to non-authoritative — churn the diff could never converge.
    bound_resolver = (
        (lambda lv: assignee_resolver(lv, jira_key)) if assignee_resolver is not None else None
    )
    changed = diff_canonical_fields(
        ticket,
        canonical_remote,
        canonical_baseline,
        outbound_mapper=outbound_mapper,
        assignee_resolver=bound_resolver,
        binding_store=binding_store,
        local_ticket_types=local_ticket_types,
        jira_key=jira_key,
        local_id=local_id,
        conflict_sink=conflict_sink,
        dropped_field_sink=dropped_field_sink,
    )
    return outbound_mapper.map_fields_to_remote(
        changed,
        ticket=ticket,
        binding_store=binding_store,
        local_ticket_types=local_ticket_types,
        status_map=status_map,
    )


def diff_canonical_fields(
    ticket: dict[str, Any],
    canonical_remote: dict[str, Any],
    canonical_baseline: dict[str, Any] | None,
    *,
    outbound_mapper: OutboundMapper,
    assignee_resolver: Callable[[str], tuple[Any, bool, bool]] | None = None,
    binding_store: Any = None,
    local_ticket_types: dict[str, str] | None = None,
    jira_key: str = "",
    local_id: str = "",
    conflict_sink: list[tuple[str, str]] | None = None,
    dropped_field_sink: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Compare a LOCAL ticket to a canonicalized remote snapshot; return the canonical
    ``changed`` dict (local field name → local value) plus the ``_assignee_is_account_id``
    sentinel when the accountId fast-path fires.

    ``canonical_remote`` / ``canonical_baseline`` are the injected InboundMapper's
    output for the current snapshot entry and the arbitration baseline (partial-
    tolerant: an absent field is simply not compared). ``outbound_mapper`` supplies
    the two vendor operations kept behind the port (description ADF fit via
    ``map_fields_to_remote``; assignee account resolution via ``resolve_assignee``).
    """
    baseline = canonical_baseline or {}
    changed: dict[str, Any] = {}

    def _suppressed_by_inbound(
        field: str, local_val: Any, *, normalized_local: str | None = None
    ) -> bool:
        """Directionality guard: local unchanged since baseline → leave the (differing)
        remote for the inbound differ instead of local-wins clobbering it. Partial-
        tolerant: a field the baseline does not carry never suppresses."""
        if field not in _INBOUND_MIRRORED_FIELDS or field not in baseline:
            return False
        if field == "assignee":
            return _assignee_matches(
                local_val, baseline.get("assignee"), baseline.get("assignee_identity")
            )
        return _baseline_form_matches(field, local_val, normalized_local, baseline)

    # A live snapshot entry is authoritative for the always-present Jira fields: an
    # absent key means the remote value is that field's natural empty default (the
    # pre-625b differ compared against ``_extract_jira_field(...) -> ""``), so a sparse
    # entry still diffs. ``status``/``parent``/``reporter`` are genuinely optional and
    # stay partial-tolerant (compared only when their source is present / local drives).

    # --- title (inbound-mirrored per ADR 0026; local==baseline defers to inbound) ---
    local_title = ticket.get("title") or ""
    if not _suppressed_by_inbound("title", local_title) and not _text_matches(
        local_title, canonical_remote.get("title", "")
    ):
        changed["title"] = local_title

    # --- description (inbound-mirrored; ADF-fit via the outbound port) ---
    local_desc = ticket.get("description") or ""
    # The local body as it will READ once Jira stores it. Computed BEFORE the
    # directionality guard so that guard can match on it as well as on the raw text:
    # under a lossy one-way rich-text codec the baseline holds ``decode(baseline_wire)``
    # while the local body is raw Markdown, so a raw-only compare reports "local
    # changed" on every pass and the description re-emits forever. Matching on either
    # form only ADDS a way to conclude "unchanged" (see ``_local_matches_baseline``), so
    # the plain-codec behaviour is unchanged.
    fitted = outbound_mapper.map_fields_to_remote({"description": local_desc}, ticket=ticket).get(
        "description", local_desc
    )
    if not _suppressed_by_inbound("description", local_desc, normalized_local=fitted):
        # The port also normalizes soft-wrapped prose (the ADF encoder rejoins a
        # hard-wrapped paragraph into one), so the body that lands — and that a later
        # fetch decodes back — is the NORMALIZED form. Route the remote value through
        # the same idempotent port before comparing, or a hard-wrapped local
        # description never matches its own landed form and the differ re-emits a
        # description update on every pass.
        # The REMOTE value is deliberately compared RAW. A body written by the old
        # paragraph-per-line encoder decodes back hard-wrapped; routing it through the
        # port too would make it match the normalized local value, so a stale
        # description would never be rewritten. Comparing raw re-emits it once, after
        # which the landed body decodes to the normalized form and converges.
        if not _text_matches(fitted, canonical_remote.get("description", "")):
            changed["description"] = fitted

    # --- ticket_type / issuetype: excluded from UPDATE; drop-sink only ---
    local_type = ticket.get("ticket_type", "task")
    remote_type = canonical_remote.get("ticket_type", "task")
    if (
        dropped_field_sink is not None
        and jira_key
        and local_type
        and remote_type
        and str(local_type).lower() != str(remote_type).lower()
    ):
        dropped_field_sink.append((jira_key, "issuetype"))

    # --- priority (inbound-mirrored) ---
    local_pri = ticket.get("priority", 2)
    if not _suppressed_by_inbound("priority", local_pri) and local_pri != canonical_remote.get(
        "priority", 2
    ):
        changed["priority"] = local_pri

    # --- status (inbound-mirrored; partial — compared only when the remote maps one) ---
    if "status" in canonical_remote:
        local_status = ticket.get("status", "open")
        if (
            not _suppressed_by_inbound("status", local_status)
            and local_status != canonical_remote["status"]
        ):
            changed["status"] = local_status

    # --- assignee (inbound-mirrored; identity match then account resolver) ---
    local_assignee = ticket.get("assignee") or ""
    remote_scalar = canonical_remote.get("assignee")
    identity = canonical_remote.get("assignee_identity")
    if not _suppressed_by_inbound("assignee", local_assignee) and not _assignee_matches(
        local_assignee, remote_scalar, identity
    ):
        value, authoritative, is_account_id = outbound_mapper.resolve_assignee(
            local_assignee, identity, assignee_resolver=assignee_resolver
        )
        if authoritative and value is None:
            pass  # converged — the resolved identity already matches remote; emit nothing
        else:
            changed["assignee"] = value
            if authoritative and is_account_id and value is not None:
                changed["_assignee_is_account_id"] = True

    # --- parent (driven by LOCAL parent state; local-wins SET, managed-gated CLEAR) ---
    suppressed_parents: list[str] = []
    present, local_parent = _resolve_local_parent(
        ticket, binding_store, local_ticket_types, suppressed_out=suppressed_parents
    )
    # Bug 8b25's guard drops a non-epic parent silently, and before ticket 9f26 that silence
    # was total: the field never entered `changed`, so `dispatch_one._update_one_apply_parent`
    # never ran and the whole `record_parent_divergence` / bridge_alerts apparatus was
    # unreachable — the pass exited 0 reporting convergence while the edit was gone.
    # Report it on the EXISTING drop channel, but ONLY when it actually costs something: if
    # the tracker already carries the parent the user asked for, nothing was dropped. That is
    # the common case (an inbound-mirrored sub-task's parent is a non-epic and already
    # correct), and alerting on it would fire every pass for every such ticket. Same rule the
    # sibling `issuetype` drop follows: record when the local value DIFFERS from the remote.
    if suppressed_parents and dropped_field_sink is not None and jira_key:
        if suppressed_parents[0] != (canonical_remote.get("remote_parent_id") or None):
            dropped_field_sink.append((jira_key, "parent"))
    if present:
        remote_parent = canonical_remote.get("remote_parent_id")
        if local_parent != remote_parent:
            if (
                not local_parent
                and remote_parent
                and not _parent_clear_is_managed(remote_parent, ticket, binding_store)
            ):
                pass  # never managed this remote parent → adopt inbound, don't clear
            elif _peer_removed_the_parent(
                local_parent, remote_parent, ticket, binding_store, local_id
            ):
                # peer de-parented, local unchanged → inbound owns the clear
                pass  # (ticket 339a-57ac-e5f3-4718)
            else:
                changed["parent"] = local_parent

    # --- both-sides conflict observability (local-wins unchanged) ---
    if conflict_sink is not None and canonical_baseline and jira_key:
        for fname in list(changed):
            if (
                fname in _INBOUND_MIRRORED_FIELDS
                and not _local_matches_baseline(fname, ticket, baseline, normalized_local=fitted)
                and not _remote_matches_baseline(fname, canonical_remote, baseline)
            ):
                conflict_sink.append((jira_key, fname))

    # --- reporter (one-way; outside the mirrored-field guards) ---
    _diff_reporter(ticket, canonical_remote.get("reporter_identity"), changed)
    return changed


def _baseline_form_matches(
    field: str, raw_local: Any, normalized_local: str | None, baseline: dict[str, Any]
) -> bool:
    """Whether ``raw_local`` — or the form it takes once Jira stores it — equals baseline.

    Under a lossy one-way rich-text codec the baseline holds ``decode(baseline_wire)``
    while the local body is raw Markdown, so a raw-only compare reports "changed" on
    every pass. Accepting EITHER form only ADDS a way to conclude "unchanged", so it
    cannot start missing an edit the raw compare already caught, and it cannot hide a
    real edit: if the normalized local equals the baseline, the edit makes no difference
    to what Jira would store.
    """
    if _text_matches(raw_local, baseline.get(field)):
        return True
    if field != "description" or normalized_local is None:
        return False
    return _text_matches(normalized_local, baseline.get(field))


def _local_matches_baseline(
    field: str,
    ticket: dict[str, Any],
    baseline: dict[str, Any],
    normalized_local: str | None = None,
) -> bool:
    """Whether the LOCAL value for a mirrored field equals the canonical baseline.

    ``normalized_local`` is the local body as it will READ once Jira has stored it
    (``normalize_outbound(fit_outbound(local))``, from the outbound port). It is an
    ALTERNATIVE match, never a replacement: under a lossy one-way rich-text codec the
    baseline holds ``decode(baseline_wire)`` while the local body is raw Markdown, so
    the two differ in FORM on every pass and the differ would re-emit forever even
    though nothing changed.

    Matching on EITHER form is deliberately conservative — it can only ADD a way to
    conclude "unchanged", so no previously-detected edit starts being missed. It also
    cannot hide a real edit: if the normalized local equals the baseline then the edit
    makes no difference to what Jira would store, so there is nothing to send.
    """
    if field not in baseline:
        return True
    if field == "assignee":
        return _assignee_matches(
            ticket.get("assignee") or "",
            baseline.get("assignee"),
            baseline.get("assignee_identity"),
        )
    local_val = {
        "title": ticket.get("title") or "",
        "description": ticket.get("description") or "",
        "priority": ticket.get("priority", 2),
        "status": ticket.get("status", "open"),
    }[field]
    return _baseline_form_matches(field, local_val, normalized_local, baseline)


def _remote_matches_baseline(
    field: str, canonical_remote: dict[str, Any], baseline: dict[str, Any]
) -> bool:
    """Whether the canonical REMOTE value for a mirrored field equals the baseline
    (``True`` — no detectable remote edit — when either lacks the field)."""
    if field not in baseline or field not in canonical_remote:
        return True
    if field == "assignee":
        r = canonical_remote.get("assignee_identity") or {}
        b = baseline.get("assignee_identity") or {}
        return r == b
    return _text_matches(canonical_remote.get(field), baseline.get(field))


def emit_baseline_cold_start(binding_store: Any, local_id: str, raw_baseline: Any) -> None:
    """Emit the one-line cold-start RECON diagnostic (story d6bd) when a confirmed
    binding still has no baseline — the one-pass arbitration warm-up window where a
    concurrent remote edit could be lost until the baseline populates."""
    if raw_baseline is None and binding_store is not None and local_id:
        if not binding_store.is_pending(local_id):
            print(
                f"RECON: baseline_cold_start local_id={local_id}",
                file=sys.stderr,
                flush=True,
            )
