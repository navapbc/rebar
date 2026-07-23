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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rebar_reconciler._backend import OutboundMapper

# Fields the INBOUND differ mirrors Jira→local. A Jira-side change to one of
# these, when local is unchanged since the last sync (matches the baseline),
# flows inbound rather than being reverted by local-wins. NOTE: ``title`` is
# deliberately absent — the pre-625b differ iterated the Jira-shaped mapped
# fields, whose title key is ``summary`` (never a member of the local-named
# set), so title was never inbound-suppressed. Preserving that exact behaviour,
# the canonical set is the four fields whose local and Jira names coincided.
_INBOUND_MIRRORED_FIELDS = frozenset({"description", "priority", "status", "assignee"})


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
    """
    if binding_store is None:
        return (False, None)
    local_parent_id = ticket.get("parent_id") or None
    if local_parent_id:
        if local_ticket_types is not None and local_parent_id in local_ticket_types:
            parent_type = (local_ticket_types.get(local_parent_id) or "").lower()
            if parent_type != "epic":
                return (False, None)  # Jira permits only Epic parents — suppress (8b25)
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
) -> dict[str, Any]:
    """Canonicalize the snapshot entry (and arbitration baseline) via the injected
    ``InboundMapper``, diff in LOCAL shape, and map the changed subset back to the
    backend's field shapes via the ``OutboundMapper`` — the whole vendor-neutral field
    path for one bound ticket. Returns the vendor-shaped ``OutboundMutation.fields``.

    Storage stays vendor-shaped: the baseline is mapped at READ time only. The
    client-backed account resolver (bound to this remote key) is threaded to the
    outbound mapper so ``resolve_assignee`` can consult the live account search.
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
    if assignee_resolver is not None:
        try:
            outbound_mapper._assignee_resolver = (  # type: ignore[attr-defined]
                lambda lv: assignee_resolver(lv, jira_key)
            )
        except (AttributeError, TypeError):
            pass  # a mapper that forbids attribute injection keeps its own resolution
    changed = diff_canonical_fields(
        ticket,
        canonical_remote,
        canonical_baseline,
        outbound_mapper=outbound_mapper,
        binding_store=binding_store,
        local_ticket_types=local_ticket_types,
        jira_key=jira_key,
        local_id=local_id,
        conflict_sink=conflict_sink,
        dropped_field_sink=dropped_field_sink,
    )
    return outbound_mapper.map_fields_to_remote(
        changed, ticket=ticket, binding_store=binding_store, local_ticket_types=local_ticket_types
    )


def diff_canonical_fields(
    ticket: dict[str, Any],
    canonical_remote: dict[str, Any],
    canonical_baseline: dict[str, Any] | None,
    *,
    outbound_mapper: OutboundMapper,
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

    def _suppressed_by_inbound(field: str, local_val: Any) -> bool:
        """Directionality guard: local unchanged since baseline → leave the (differing)
        remote for the inbound differ instead of local-wins clobbering it. Partial-
        tolerant: a field the baseline does not carry never suppresses."""
        if field not in _INBOUND_MIRRORED_FIELDS or field not in baseline:
            return False
        if field == "assignee":
            return _assignee_matches(
                local_val, baseline.get("assignee"), baseline.get("assignee_identity")
            )
        return _text_matches(local_val, baseline.get(field))

    # A live snapshot entry is authoritative for the always-present Jira fields: an
    # absent key means the remote value is that field's natural empty default (the
    # pre-625b differ compared against ``_extract_jira_field(...) -> ""``), so a sparse
    # entry still diffs. ``status``/``parent``/``reporter`` are genuinely optional and
    # stay partial-tolerant (compared only when their source is present / local drives).

    # --- title (never inbound-suppressed; see _INBOUND_MIRRORED_FIELDS note) ---
    local_title = ticket.get("title") or ""
    if not _text_matches(local_title, canonical_remote.get("title", "")):
        changed["title"] = local_title

    # --- description (inbound-mirrored; ADF-fit via the outbound port) ---
    local_desc = ticket.get("description") or ""
    # Directionality uses the RAW local text (matches the pre-625b ordering, where the
    # fit was applied only just before the emit compare).
    if not _suppressed_by_inbound("description", local_desc):
        fitted = outbound_mapper.map_fields_to_remote(
            {"description": local_desc}, ticket=ticket
        ).get("description", local_desc)
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
            local_assignee, identity
        )
        if authoritative and value is None:
            pass  # converged — the resolved identity already matches remote; emit nothing
        else:
            changed["assignee"] = value
            if authoritative and is_account_id and value is not None:
                changed["_assignee_is_account_id"] = True

    # --- parent (driven by LOCAL parent state; local-wins SET, managed-gated CLEAR) ---
    present, local_parent = _resolve_local_parent(ticket, binding_store, local_ticket_types)
    if present:
        remote_parent = canonical_remote.get("remote_parent_id")
        if local_parent != remote_parent:
            if (
                not local_parent
                and remote_parent
                and not _parent_clear_is_managed(remote_parent, ticket, binding_store)
            ):
                pass  # never managed this remote parent → adopt inbound, don't clear
            else:
                changed["parent"] = local_parent

    # --- both-sides conflict observability (local-wins unchanged) ---
    if conflict_sink is not None and canonical_baseline and jira_key:
        for fname in list(changed):
            if (
                fname in _INBOUND_MIRRORED_FIELDS
                and not _local_matches_baseline(fname, ticket, baseline)
                and not _remote_matches_baseline(fname, canonical_remote, baseline)
            ):
                conflict_sink.append((jira_key, fname))

    # --- reporter (one-way; outside the mirrored-field guards) ---
    _diff_reporter(ticket, canonical_remote.get("reporter_identity"), changed)
    return changed


def _local_matches_baseline(field: str, ticket: dict[str, Any], baseline: dict[str, Any]) -> bool:
    """Whether the LOCAL value for a mirrored field equals the canonical baseline."""
    if field not in baseline:
        return True
    if field == "assignee":
        return _assignee_matches(
            ticket.get("assignee") or "",
            baseline.get("assignee"),
            baseline.get("assignee_identity"),
        )
    local_val = {
        "description": ticket.get("description") or "",
        "priority": ticket.get("priority", 2),
        "status": ticket.get("status", "open"),
    }[field]
    return _text_matches(local_val, baseline.get(field))


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
            print(  # noqa: T201 — operator-facing RECON: cold-start diagnostic on stderr
                f"RECON: baseline_cold_start local_id={local_id}",
                file=sys.stderr,
                flush=True,
            )
