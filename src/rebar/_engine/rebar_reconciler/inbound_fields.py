"""Jira -> local field/status translation for the inbound differ.

This is the self-contained *translation* layer extracted from ``inbound_differ``
(module-size split, epic 716f): the hand-maintained Jira-issuetype / priority /
workflow-status maps and the pure helpers that turn a raw Jira ``fields`` dict
into the local ticket field/value shape the differ then diffs against.

It is a LEAF: every function here references only other symbols in this module
(and the sibling ``adf`` module, loaded lazily by-path). ``inbound_differ``
imports these names back and re-exports them, so ``inbound_differ.<symbol>``
attribute access (and the config parity tests) keep resolving unchanged.

This module is pure: no I/O, no time/random, no logging, no globals beyond the
lazy ``adf`` module cache.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ADF_KEY_INBOUND = "rebar_reconciler.adapters.jira.adf"
_AdfModule_Inbound = None


def _load_adf():
    """Lazy-load the sibling adf module (mirrors outbound_differ._load_adf)."""
    global _AdfModule_Inbound
    if _AdfModule_Inbound is not None:
        return _AdfModule_Inbound
    if _ADF_KEY_INBOUND in sys.modules:
        _AdfModule_Inbound = sys.modules[_ADF_KEY_INBOUND]
        return _AdfModule_Inbound
    adf_path = Path(__file__).parent / "adapters" / "jira" / "adf.py"
    spec = importlib.util.spec_from_file_location(_ADF_KEY_INBOUND, adf_path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"adf.py not found at {adf_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_ADF_KEY_INBOUND] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _AdfModule_Inbound = mod
    return mod


# ---------------------------------------------------------------------------
# Field mapping constants (Jira -> local)
# ---------------------------------------------------------------------------

_JIRA_TO_LOCAL_TYPE: dict[str, str] = {
    "Bug": "bug",
    "Story": "story",
    "Task": "task",
    "Epic": "epic",
}

_JIRA_TO_LOCAL_PRIORITY: dict[str, int] = {
    "Highest": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
    "Lowest": 4,
}

_JIRA_TO_LOCAL_STATUS: dict[str, str] = {
    # Kept in lock-step with config.jira_to_local_status by the parity test.
    "IDEA": "idea",
    "To Do": "open",
    "In Progress": "in_progress",
    # "In Review" is a live DIG workflow state that was missing from the map,
    # causing it to fall through to the "open" default (ticket 929a).
    "In Review": "in_progress",
    "Blocked": "blocked",
    "Done": "closed",
    "Cancelled": "cancelled",
}

# rebar-status: annotation labels that override the Jira workflow status on inbound.
# Maps rebar-status:<label> -> local status. Takes precedence over _JIRA_TO_LOCAL_STATUS.
_REBAR_STATUS_LABEL_TO_LOCAL: dict[str, str] = {
    "rebar-status:blocked": "blocked",
    "rebar-status:cancelled": "cancelled",
}


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def _extract_jira_field_value(jira_fields: dict[str, Any], field: str) -> Any:
    """Extract a Jira field value, handling nested structures."""
    raw = jira_fields.get(field)
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw.get("name", raw.get("displayName", ""))
    return raw


def _assignee_matches(local_val: str, jira_raw: Any) -> bool:
    """Permissive assignee equality (mirror of outbound_differ._assignee_matches).

    Convergence-churn fix (bug 85a1 family): a live Jira fetch returns
    ``assignee`` as ``{accountId, displayName, emailAddress}``; local tickets
    store assignee as a bare string that may be an email (ticket-create
    default), a displayName (probe), or "Test" (git-config default). The
    outbound differ already tolerates all three identity forms; without the
    same tolerance here, the inbound differ extracts only ``displayName`` and
    reports a phantom ``assignee`` change on every pass whenever local stores a
    DIFFERENT identity form than Jira returns — the assignee field never
    converges.

    Treat ``local_val`` as matching when it equals ANY of {emailAddress,
    accountId, displayName}. Both sides empty (unassigned) also match.
    """
    if jira_raw is None:
        return (local_val or "") == ""
    if not isinstance(jira_raw, dict):
        return (local_val or "") == str(jira_raw)
    candidates = {
        (jira_raw.get("emailAddress") or "").strip(),
        (jira_raw.get("accountId") or "").strip(),
        (jira_raw.get("displayName") or "").strip(),
    }
    candidates.discard("")
    return (local_val or "").strip() in candidates


def _normalize_jira_body(body: Any) -> str:
    """Coerce a Jira comment body (ADF dict or string) to plain text.

    The reconciler marker token is preserved (callers filter on it).
    """
    if isinstance(body, dict):
        return _load_adf().adf_to_text(body)
    return str(body) if body is not None else ""


def _map_jira_to_local_fields(jira_fields: dict[str, Any]) -> dict[str, Any]:
    """Map Jira fields to local ticket field names/values.

    ticket 929a: when jira_fields carries a rebar-status: annotation label
    (e.g. ``rebar-status:blocked``), the label takes precedence over the raw
    Jira workflow status for the local status mapping. This preserves lossless
    round-trip for statuses that have no direct Jira equivalent (blocked maps
    to In Progress on Jira, cancelled maps to Done). Without this, a
    blocked→In Progress outbound followed by an inbound pass would silently
    overwrite local "blocked" with "in_progress".
    """
    summary = _extract_jira_field_value(jira_fields, "summary") or ""
    # Bug 1bb2: ``_extract_jira_field_value`` returns nested dicts verbatim
    # for any field that isn't a {.name/.displayName} object — Jira's
    # ``description`` is an ADF (Atlassian Document Format) dict in cloud
    # tenants. Normalize to plain text here so the diff map carries a
    # string and the applier writes a string into the local EDIT event.
    description_raw = jira_fields.get("description")
    description = _normalize_jira_body(description_raw) if description_raw else ""
    issuetype_raw = _extract_jira_field_value(jira_fields, "issuetype") or "Task"
    priority_raw = _extract_jira_field_value(jira_fields, "priority") or "Medium"
    status_raw = _extract_jira_field_value(jira_fields, "status") or "To Do"
    assignee = _extract_jira_field_value(jira_fields, "assignee") or ""

    # Prefer rebar-status: annotation label over raw Jira workflow status.
    # Check labels list for any rebar-status: entry and map to local status.
    local_status: str | None = None
    for label in jira_fields.get("labels") or []:
        if label in _REBAR_STATUS_LABEL_TO_LOCAL:
            local_status = _REBAR_STATUS_LABEL_TO_LOCAL[label]
            break
    if local_status is None:
        # Bug 5886: an unmapped Jira status must NOT default to "open" (that silently
        # reopened closed tickets). Leave it None so the dict omits status → no diff.
        local_status = _JIRA_TO_LOCAL_STATUS.get(status_raw)

    return {
        "title": summary,
        "description": description,
        "ticket_type": _JIRA_TO_LOCAL_TYPE.get(issuetype_raw, "task"),
        "priority": _JIRA_TO_LOCAL_PRIORITY.get(priority_raw, 2),
        "assignee": assignee,
        **({"status": local_status} if local_status is not None else {}),
    }
