"""Local→Jira field mapping for the Jira Cloud backend.

Owns ``_map_local_to_jira_fields``, which maps a local ticket's fields to Jira
field names and values for a CREATE body (and, with ``emit_detach_clear``, the
parent-clear sentinel). ``JiraBackend``'s outbound mapper is its only production
caller. The module also re-exports the local→Jira priority, status and type maps
under their historical private names, because tests and the jira-family parity
guards read them as module attributes.

The UPDATE field diff does not live here: the pass diffs in local shape through
``rebar.reconciler.outbound_field_diff.compute_update_fields``.
"""

from __future__ import annotations

from typing import Any

# Story J2 (epic e369) de-duplicated the local<->Jira priority/status maps against
# the drifted copy that used to live in ``adapters/jira/jira_fields.py`` —
# ``adapters/jira_family/value_maps.py`` is now the SOLE definition site under
# ``adapters/`` for both maps. Imported (not re-declared) so this module and the
# ACLI transport resolve to the identical object; kept under the historical local
# names so this module's own call sites below need no further change. Absolute
# import, the package convention.
#
# Kept as a MODULE ATTRIBUTE (read by tests and the state/test_config parity guard, and
# pinned identical across the jira-family) even though S5 now routes the priority value
# through ``resolve_outbound_priority`` (map-or-drift) rather than subscripting this map
# directly. ``resolve_outbound_priority`` builds its own str-keyed view of this same
# built-in object when no per-project map is supplied, so the no-config behaviour is
# unchanged.
from rebar.reconciler.adapters.jira_family import (  # noqa: F401
    LOCAL_PRIORITY_TO_JIRA as _LOCAL_TO_JIRA_PRIORITY,
)

# Kept as a MODULE ATTRIBUTE (read by tests and the state/test_config parity guard, and
# pinned identical to ``LOCAL_STATUS_TO_JIRA`` by the jira-family boundary test) even
# though S2 routes the status value through ``resolve_outbound_status`` (map-or-drift)
# rather than subscripting this map directly. ``resolve_outbound_status`` falls back to
# the same built-in object when no per-project map is supplied, so the no-config
# behaviour is unchanged.
from rebar.reconciler.adapters.jira_family import (  # noqa: F401
    LOCAL_STATUS_TO_JIRA as _LOCAL_TO_JIRA_STATUS,
)

# Story bd9e (epic 3e73) extended the same de-duplication to the local->Jira TYPE
# map, whose second copy lived in ``adapters/jira_datacenter/backend.py``; both
# copies were content-identical, so the move is behaviour-preserving. Kept under
# the historical private name because it is read as a MODULE ATTRIBUTE by
# ``tests/.../diffing/test_outbound_differ_session_log_exclusion.py``. Absolute
# import, as above.
from rebar.reconciler.adapters.jira_family import (  # noqa: F401
    LOCAL_TYPE_TO_JIRA as _LOCAL_TO_JIRA_TYPE,
)
from rebar.reconciler.adapters.jira_family.outbound_mapper import (
    merge_create_defaults,
    resolve_outbound_priority,
    resolve_outbound_status,
    resolve_outbound_type,
)

# ---------------------------------------------------------------------------
# Field mapping helpers
# ---------------------------------------------------------------------------


def _map_local_to_jira_fields(
    ticket: dict[str, Any],
    binding_store: Any = None,
    local_ticket_types: dict[str, str] | None = None,
    emit_detach_clear: bool = False,
    *,
    suppressed_out: list[str] | None = None,
    status_map: dict[str, str] | None = None,
    type_map: dict[str, str] | None = None,
    priority_map: dict[str, str] | None = None,
    create_defaults: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Map local ticket fields to Jira field names/values.

    Use ``.get(key) or default`` (not ``.get(key, default)``) for string
    fields so an explicit ``None`` value normalises to the empty-string
    default. ``.get(key, default)`` only falls back when the key is
    MISSING — if the key exists with value ``None`` (e.g. unassigned
    tickets where the ticket reducer initialises ``assignee: None``),
    .get returns None, not the default. None would then become the
    literal string ``"None"`` after str() conversion at the ACLI boundary,
    causing ACLI to reject the edit with exit 1.

    Parent resolution (ticket 8b25): when ``binding_store`` is supplied and
    the ticket carries a ``parent_id``, attempt to resolve it to a Jira key
    via ``binding_store.get_jira_key(parent_id)``.  An unbound parent is
    silently omitted from the returned dict (not ``None`` / empty-string) so
    the diff layer can distinguish "no parent set" from "parent set but
    unbound this pass" and skip / retry accordingly.

    ``suppressed_out`` (ticket 8390) is how a caller learns that the 8b25
    hierarchy guard just dropped a parent: the BOUND non-epic case appends the
    remote key this call WOULD have emitted. It reports the fact and judges
    nothing — same split ``_resolve_local_parent`` uses on the UPDATE side
    (ticket 9f26), because only the caller knows whether the suppression cost
    anything. An UNBOUND parent appends NOTHING: it has not been offered to the
    tracker yet and converges on a later pass, so alerting on it is churn.

    The returned dict is unchanged by this parameter on every input — nothing
    rebar sends to Jira depends on it.

    ``priority_map`` (S5) is the effective per-project local->Jira priority map
    (``config.effective_priority_map``); ``None`` falls back to the built-in map. A local
    priority with NO target is OMITTED (map-or-drift), never coerced to ``"Medium"``.
    ``create_defaults`` (S5) is a str-valued map of required-beyond-baseline vendor fields
    merged UNDER this computed body (baseline computed fields win on collision), CREATE-only.
    """
    result: dict[str, Any] = {
        "summary": ticket.get("title") or "",
        "description": ticket.get("description") or "",
        "issuetype": resolve_outbound_type(ticket.get("ticket_type", "task"), type_map),
        "assignee": ticket.get("assignee") or "",
    }
    # Map-or-drift (S5): a local priority with NO Jira target is OMITTED entirely
    # (Jira priority left unchanged), never coerced to "Medium".
    _priority_target = resolve_outbound_priority(ticket.get("priority", 2), priority_map)
    if _priority_target is not None:
        result["priority"] = _priority_target
    # Map-or-drift (S2): a local status with NO Jira target is OMITTED entirely
    # (Jira status left unchanged), never coerced to "To Do".
    _status_target = resolve_outbound_status(ticket.get("status", "open"), status_map)
    if _status_target is not None:
        result["status"] = _status_target
    # Parent sync (ticket 8b25): resolve local parent_id → Jira key.
    # Omit the key entirely when unbound so the diff skips it (retry next pass).
    local_parent_id = ticket.get("parent_id") or None
    if local_parent_id and binding_store is not None:
        import logging as _logging

        # Hierarchy pre-check (ticket 8b25): on this next-gen Jira project only
        # an Epic may be a parent. A non-epic parent (e.g. Task→Task) is
        # rejected by Jira with HTTP 400. Suppress the parent diff entirely
        # when the resolved local parent's ticket_type != "epic" so the differ
        # never perpetually re-emits a parent mutation Jira will always reject.
        # This is a sync exclusion mirroring the bug-36af issuetype pattern.
        # When parent-type info is unavailable (map not supplied / parent
        # absent from the map), fall through to the existing behaviour rather
        # than guess — the applier-side 400-skip remains the backstop.
        if local_ticket_types is not None and local_parent_id in local_ticket_types:
            parent_type = (local_ticket_types.get(local_parent_id) or "").lower()
            if parent_type != "epic":
                _logging.getLogger(__name__).debug(
                    "_map_local_to_jira_fields: parent_id=%r for ticket %r is a "
                    "non-epic (%r); suppressing parent diff (Jira hierarchy only "
                    "permits Epic parents — sync exclusion, ticket 8b25)",
                    local_parent_id,
                    ticket.get("ticket_id"),
                    parent_type,
                )
                # Ticket 8390: report the suppression so the create is not silent.
                # At CREATE the issue does not exist on the tracker yet, so a
                # dropped parent is unconditionally a real loss of hierarchy —
                # but only a BOUND parent has a key we could ever have sent; an
                # unbound one converges later and is not a drop.
                if suppressed_out is not None:
                    suppressed_key = binding_store.get_jira_key(local_parent_id)
                    if suppressed_key:
                        suppressed_out.append(suppressed_key)
                return merge_create_defaults(create_defaults, result)

        jira_parent_key = binding_store.get_jira_key(local_parent_id)
        if jira_parent_key:
            result["parent"] = jira_parent_key
        else:
            _logging.getLogger(__name__).debug(
                "_map_local_to_jira_fields: parent_id=%r for ticket %r is unbound "
                "this pass; skipping parent field (will retry next pass)",
                local_parent_id,
                ticket.get("ticket_id"),
            )
    elif binding_store is not None and emit_detach_clear:
        # Symmetric parent CLEAR (parent-detach churn fix): the ticket has been
        # DETACHED locally (parent_id is falsy). Emit an explicit ``None``
        # sentinel into the mapped dict so the field-diff loop's ``parent``
        # branch runs and compares None against the Jira-side parent key:
        # it emits a CLEAR only when Jira still carries a parent (stale epic-
        # link) and nothing when both sides are already parent-less (so a
        # never-parented ticket does not churn a clear every pass).
        # Gated on binding_store (the parent-sync feature seam) AND on
        # ``emit_detach_clear`` — only an UPDATE-shaped caller opts in; the
        # CREATE path leaves the key ABSENT so an orphan create
        # never carries a spurious ``parent: None`` payload. The hierarchy
        # pre-check above is intentionally skipped for a clear — there is no
        # parent type to validate.
        result["parent"] = None
    return merge_create_defaults(create_defaults, result)
