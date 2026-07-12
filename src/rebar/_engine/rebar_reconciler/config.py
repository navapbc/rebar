"""Configuration constants for rebar_reconciler."""

from __future__ import annotations

EXCLUDED_FIELDS: tuple[str, ...] = ("local_id", "rebar-id")

# Local ticket types that are NEVER synced to Jira. session_log and code_review
# tickets are verbose, local, agent-facing artifacts with no place in a Jira project,
# so compute_outbound_mutations skips them entirely (alongside the excluded-status
# check). These types are also deliberately ABSENT from outbound_differ's
# _LOCAL_TO_JIRA_TYPE map so any leak past this filter surfaces rather than
# silently syncing.
EXCLUDED_SYNC_TYPES: frozenset[str] = frozenset({"session_log", "code_review", "identity"})

# Status mapping: local-side status name -> Jira-side status name.
# Used by outbound_update v1's status-routing path (gated behind
# REBAR_RECONCILER_STATUS_GATING) and by the preflight status-mapping scan
# in reconcile.py — preflight aborts a pass when any update mutation
# references a status absent from this mapping. An empty dict is a valid
# kill-switch — preflight tolerates an empty mapping when no update
# mutations contain a status field.
local_to_jira_status: dict[str, str] = {
    # `idea ↔ IDEA` is a UNIQUE (injective) mapping — no rebar-status: annotation
    # label is needed to reconstruct it inbound. Requires the Jira project workflow
    # to define an `IDEA` status with transitions into/out of it (operator
    # prerequisite — see docs/jira-sync-setup.md "The `idea` status ↔ Jira `IDEA`").
    "idea": "IDEA",
    "open": "To Do",
    "in_progress": "In Progress",
    # blocked/cancelled have no direct equivalent in the live DIG workflow
    # ({To Do, In Progress, In Review, Done} only). Map to the nearest live
    # state; lossless information is preserved via rebar-status: annotation
    # labels emitted/removed by status logic (outbound_differ).
    "blocked": "In Progress",
    "closed": "Done",
    "cancelled": "Done",
    "deleted": "Done",
}

# Canonical reverse mapping: Jira workflow status -> local status. This is
# NOT derivable from local_to_jira_status (the forward map is non-injective:
# blocked/in_progress both map to "In Progress", closed/cancelled/deleted all
# map to "Done"). The canonical preimage is the UNANNOTATED local status —
# blocked/cancelled are reconstructed from rebar-status: annotation labels by
# callers, never from the workflow status alone. Deriving the reverse map by
# inverting local_to_jira_status (as applier._jira_status_to_local once did,
# with lexicographic tie-breaking) imported "In Progress" as blocked and
# "Done" as cancelled — ticket robe-creek-zealot.
#
# Must stay in lock-step with inbound_differ._JIRA_TO_LOCAL_STATUS (parity
# is enforced by tests/unit/rebar_reconciler/test_config.py).
jira_to_local_status: dict[str, str] = {
    "IDEA": "idea",
    "To Do": "open",
    "In Progress": "in_progress",
    # "In Review" is a live DIG workflow state with no local equivalent;
    # nearest local state (matches inbound_differ, ticket 929a).
    "In Review": "in_progress",
    "Blocked": "blocked",
    "Done": "closed",
    "Cancelled": "cancelled",
}
