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

# Preserve the status-map module attribute for parity tests; the outbound
# resolver uses the same built-in map without a project override.
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
    """Map local fields to a Jira create body.

    Empty/None strings use empty defaults. Resolve bound parents to Jira keys;
    omit unbound ones for a later retry. ``suppressed_out`` records a bound
    non-epic parent omitted by the hierarchy guard without changing the body.
    Unmapped priorities are omitted. ``create_defaults`` fills only fields the
    computed body lacks.
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

        # Jira accepts only Epic parents here. Suppress known non-epic parents;
        # without type data, let the applier's HTTP 400 guard handle rejection.
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
                # Report a suppressed bound parent; an unbound one can converge
                # later and has no key to report.
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
