"""Configuration constants for dso_reconciler."""
from __future__ import annotations

EXCLUDED_FIELDS: tuple[str, ...] = ('dso_local_id', 'dso-id')

# Status mapping: local-side status name -> Jira-side status name.
# Used by outbound_update v1's status-routing path (gated behind
# DSO_RECONCILER_STATUS_GATING) and by the preflight status-mapping scan
# in reconcile.py — preflight aborts a pass when any update mutation
# references a status absent from this mapping. An empty dict is a valid
# kill-switch — preflight tolerates an empty mapping when no update
# mutations contain a status field.
local_to_jira_status: dict[str, str] = {
    "open": "To Do",
    "in_progress": "In Progress",
    # blocked/cancelled have no direct equivalent in the live DIG workflow
    # ({To Do, In Progress, In Review, Done} only). Map to the nearest live
    # state; lossless information is preserved via dso-status: annotation
    # labels emitted/removed by status logic (outbound_differ).
    "blocked": "In Progress",
    "closed": "Done",
    "cancelled": "Done",
    "deleted": "Done",
}
