"""Compare outbound fields in canonical local shape.

The inbound mapper canonicalizes the remote snapshot and baseline; the
outbound mapper converts changed local fields back to vendor shape. Local-wins,
inbound directionality, managed parent clears, identity resolution, and
conflict/drop alerts remain in this neutral core without vendor imports.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rebar.reconciler._backend import OutboundMapper

# These 5 mirrored fields defer to inbound when local still matches baseline;
# title is included so a remote summary edit is not reverted (ADR 0026).
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
    """Match any canonical remote identity form, including both-empty."""
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
    """Resolve a parent for update as ``(present, remote_key_or_none)``.

    Bound epic or unknown type emits its key; local detachment proposes a
    clear; unbound and non-epic parents are omitted. Only a bound non-epic
    suppression is reported through ``suppressed_out``. The caller compares it
    with remote state before alerting, so an already-correct parent stays quiet.
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
    """Let inbound clear a parent only when the peer removed an observed one.

    If the last observed parent equals the current local parent and remote is
    now empty, outbound stands down. A locally new parent still wins. Older
    stores without peer-parent evidence retain their prior local-wins behavior.
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
    priority_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Canonicalize remote and baseline, diff locally, then map changed fields out.

    Baseline storage stays vendor-shaped; conversion happens on read. Bind the
    account resolver to this Jira key as an explicit mapper collaborator.
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
    # Bind the caller's assignee resolver to this Jira key as a diff argument;
    # mapper attribute assignment could silently lose live account search.
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
        priority_map=priority_map,
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
    """Return changed local fields and the account-ID sentinel when needed.

    Remote and baseline values are canonical mapper output. Missing optional
    fields remain partial-tolerant; vendor fitting and account resolution stay
    behind the outbound mapper.
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

    # Missing always-present fields use natural empty defaults and still diff;
    # status, parent, and reporter remain optional.

    # --- title (inbound-mirrored per ADR 0026; local==baseline defers to inbound) ---
    local_title = ticket.get("title") or ""
    if not _suppressed_by_inbound("title", local_title) and not _text_matches(
        local_title, canonical_remote.get("title", "")
    ):
        changed["title"] = local_title

    # --- description (inbound-mirrored; ADF-fit via the outbound port) ---
    local_desc = ticket.get("description") or ""
    # Compute the landed body before arbitration: a lossy codec can make raw
    # Markdown differ from an unchanged decoded baseline.
    fitted = outbound_mapper.map_fields_to_remote({"description": local_desc}, ticket=ticket).get(
        "description", local_desc
    )
    if not _suppressed_by_inbound("description", local_desc, normalized_local=fitted):
        # Compare the fitted local body with raw remote text. Normalizing the
        # remote too would hide legacy hard wraps that need one rewrite.
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
    # Report a suppressed non-epic parent only when remote differs from the
    # requested local parent; an already-correct remote parent is no drop.
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
    """Match raw local or its landed form against baseline.

    A lossy rich-text codec may change representation without changing what
    Jira stores; either match prevents needless re-emission.
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
    """Compare a mirrored local field with baseline, including its landed form.

    The alternate normalized match prevents lossy rich-text representation
    from causing repeated updates when the stored value is unchanged.
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
            print(  # noqa: T201
                f"RECON: baseline_cold_start local_id={local_id}",
                file=sys.stderr,
                flush=True,
            )
