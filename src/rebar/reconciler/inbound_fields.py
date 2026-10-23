"""Jira field and status translation for inbound reconciliation.
Maps raw Jira fields to local values, recovers annotation labels, normalizes
text, and classifies parent evidence. The inbound differ re-exports these
helpers for existing callers."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, get_args

from rebar.types import TicketType

if TYPE_CHECKING:
    from rebar.reconciler._backend import OutboundMapper

_ADF_KEY_INBOUND = "rebar.reconciler.adapters.jira.adf"
_AdfModule_Inbound = None


def _load_adf():
    """Lazy-load the sibling adf module (mirrors outbound_differ._load_adf)."""
    global _AdfModule_Inbound
    if _AdfModule_Inbound is not None:
        return _AdfModule_Inbound
    if _ADF_KEY_INBOUND in sys.modules:
        _AdfModule_Inbound = sys.modules[_ADF_KEY_INBOUND]
        return _AdfModule_Inbound
    mod = importlib.import_module("rebar.reconciler.adapters.jira.adf")
    _AdfModule_Inbound = mod
    return mod


def _text_matches(a: Any, b: Any) -> bool:
    """String comparison tolerant of trailing whitespace (Jira strips it on write),
    falling back to plain equality for non-strings. Mirror of the outbound differ's
    ``outbound_field_diff._text_matches``; replicated here so the pure inbound-diff leaf
    stays free of a cross-differ import cycle."""
    if isinstance(a, str) and isinstance(b, str):
        return a.rstrip() == b.rstrip()
    return a == b


def _description_forms(
    local_val: str,
    jira_val: Any,
    jira_raw: Any,
    local_ticket: dict[str, Any],
    outbound_mapper: OutboundMapper | None,
) -> tuple[str, Any] | None:
    """Return comparable local and Jira description forms, or None for an echo.
    With an outbound mapper, render the local body to its landed wire form first.
    This detects a configured Markdown to wiki echo without replacing the local
    Markdown. Otherwise compare ADF-normalized values. A Jira edit differing from
    the landed form still passes through."""
    if outbound_mapper is not None:
        landed = outbound_mapper.map_fields_to_remote(
            {"description": local_ticket.get("description") or ""},
            ticket=local_ticket,
        ).get("description")
        if _text_matches(landed, jira_raw):
            return None
    adf = _load_adf()
    local_norm = adf.normalize_description(adf.fit_text_to_adf_limit(local_val))
    jira_norm = adf.normalize_description(jira_val) if isinstance(jira_val, str) else jira_val
    return local_norm, jira_norm


# Jira field maps

_JIRA_TO_LOCAL_TYPE: dict[str, str] = {
    "Bug": "bug",
    "Story": "story",
    "Task": "task",
    "Epic": "epic",
}

# Valid rebar-type labels come from TicketType. Unknown suffixes fall back
# to the Jira issue type.
_LOCAL_TYPE_VOCAB: frozenset[str] = frozenset(get_args(TicketType))

_REBAR_TYPE_LABEL_PREFIX = "rebar-type:"


def recover_type_label(labels: Any) -> str | None:
    """Return the first valid rebar-type label, or None.
    Unknown suffixes are ignored so the caller can use the Jira issue type.
    The label preserves local types when several map to one Jira type."""
    for label in labels or []:
        if isinstance(label, str) and label.startswith(_REBAR_TYPE_LABEL_PREFIX):
            candidate = label[len(_REBAR_TYPE_LABEL_PREFIX) :]
            if candidate in _LOCAL_TYPE_VOCAB:
                return candidate
    return None


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

# rebar-status: annotation labels override the Jira workflow status on inbound. A
# ``rebar-status:<local>`` label (emitted outbound by the built-in-reverse stamp rule)
# recovers the local status DIRECTLY — project-key-free and reverse-map-free — for any
# status whose forward mapping was lossy. This generalises the retired 2-entry
# blocked/cancelled literal (``_REBAR_STATUS_LABEL_TO_LOCAL``): it recovers ANY valid
# local status (e.g. ``rebar-status:open``), which that literal could not.
_REBAR_STATUS_LABEL_PREFIX = "rebar-status:"

# The local-status vocabulary a ``rebar-status:<local>`` label may name. A label whose
# suffix is NOT in this set is ignored (fall back to the raw workflow status), so a
# malformed / unknown label never fabricates a bogus local status. DERIVED (not a
# hand-maintained parallel literal) from the parity-tested reverse map plus the one
# terminal local with no Jira reverse (``deleted`` -> "Done", which reverses to
# ``closed``): the recovery domain is exactly the local statuses the outbound stamp rule
# can emit. ``inbound_translate._LOCAL_STATUS_VALUES`` re-uses this so the two cannot
# drift.
_LOCAL_STATUS_VOCAB: frozenset[str] = frozenset(_JIRA_TO_LOCAL_STATUS.values()) | {"deleted"}


def recover_status_label(labels: Any) -> str | None:
    """Recover a local status from a ``rebar-status:<local>`` annotation label.

    Scans ``labels`` for the first ``rebar-status:`` label whose suffix is a valid local
    status (in :data:`_LOCAL_STATUS_VOCAB`) and returns that ``<local>``; an
    unknown/malformed suffix is ignored so the caller falls back to the raw workflow
    status. ``None`` when no valid label is present.

    The SINGLE shared implementation of the inbound status-label recovery — imported by
    ``inbound_translate`` / ``apply_inbound_events`` / ``inbound_differ`` (and re-exported
    from ``applier``) in place of the retired per-module ``_REBAR_STATUS_LABEL_TO_LOCAL``
    literals, which only recognised blocked/cancelled."""
    for label in labels or []:
        if isinstance(label, str) and label.startswith(_REBAR_STATUS_LABEL_PREFIX):
            candidate = label[len(_REBAR_STATUS_LABEL_PREFIX) :]
            if candidate in _LOCAL_STATUS_VOCAB:
                return candidate
    return None


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
    """Compare a local assignee with Jira email, account ID, or display name.
    Empty values match an unassigned Jira user. Matching the raw identity shapes
    prevents repeated inbound assignee changes."""
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


def normalize_rich_text(body: Any) -> str:
    """Decode ADF dictionaries to text. Pass strings through and map None to an
    empty string. The inbound mapper and outbound comment differ share this helper."""
    if isinstance(body, dict):
        return _load_adf().adf_to_text(body)
    return str(body) if body is not None else ""


def normalize_baseline_value(field: str, value: Any) -> Any:
    """Project a raw mirrored Jira field into its stored baseline value."""
    if field == "description":
        return normalize_rich_text(value)
    if field in ("priority", "status") and isinstance(value, dict):
        return value.get("name")
    return value


def _normalize_jira_body(body: Any) -> str:
    """Coerce a Jira comment body (ADF dict or string) to plain text.

    The reconciler marker token is preserved (callers filter on it). Thin
    alias over :func:`normalize_rich_text` (ticket 21ca).
    """
    return normalize_rich_text(body)


def _identity_of(raw: Any) -> dict[str, Any]:
    """A fixed-shape canonical identity ``{"display", "email", "account_id"}`` from a
    Jira user object (``assignee``/``reporter``). A null / non-dict value yields all
    three ``None`` — so a present-but-unassigned field is distinguishable from an
    absent one (which emits no identity key at all).

    ``account_id`` carries ``accountId`` **or**, when absent, Jira Data Center's
    ``name`` (the username) — DC has no accountId concept at all. The key is
    deliberately the EXISTING ``account_id`` rather than a new one: ``_diff_reporter``
    (``outbound_field_diff.py``) compares ``reporter_identity["account_id"]`` against
    the desired identity, so a DC username stored anywhere else leaves that
    comparison reading ``None`` on every snapshot and the reporter mutation
    re-emitting forever, even immediately after a successful write.

    **The precedence is load-bearing and ``accountId`` must win.** Measured across
    all three real payload shapes: ``{accountId}`` → the accountId (Cloud
    unchanged); ``{name}`` → the username (DC fixed); ``{accountId, name}`` → the
    accountId (Cloud does not regress). Reversing the order would silently
    re-identify every Cloud user.
    """
    if isinstance(raw, dict):
        return {
            "display": raw.get("displayName"),
            "email": raw.get("emailAddress"),
            "account_id": raw.get("accountId") or raw.get("name"),
        }
    return {"display": None, "email": None, "account_id": None}


def _map_jira_to_local_fields(jira_fields: dict[str, Any]) -> dict[str, Any]:
    """Map present Jira keys to local fields without inventing absent values.
    Present null keys retain their defaults. Valid rebar-status labels override
    workflow status. Include canonical assignee and reporter identities and the
    remote parent key while retaining the scalar assignee."""
    out: dict[str, Any] = {}

    if "summary" in jira_fields:
        out["title"] = _extract_jira_field_value(jira_fields, "summary") or ""
    if "description" in jira_fields:
        # Decode Jira ADF descriptions before the local diff and EDIT event.
        description_raw = jira_fields.get("description")
        out["description"] = _normalize_jira_body(description_raw) if description_raw else ""
    recovered_type = recover_type_label(jira_fields.get("labels"))
    if recovered_type is not None:
        # S3: a rebar-type: annotation label WINS over the raw Jira issue type. The
        # label recovers ANY valid local type directly (reverse-map-free), so a
        # per-project collapse (two local types onto one Jira type) round-trips.
        out["ticket_type"] = recovered_type
    elif "issuetype" in jira_fields:
        issuetype_raw = _extract_jira_field_value(jira_fields, "issuetype") or "Task"
        out["ticket_type"] = _JIRA_TO_LOCAL_TYPE.get(issuetype_raw, "task")
    if "priority" in jira_fields:
        priority_raw = _extract_jira_field_value(jira_fields, "priority") or "Medium"
        out["priority"] = _JIRA_TO_LOCAL_PRIORITY.get(priority_raw, 2)
    if "assignee" in jira_fields:
        out["assignee"] = _extract_jira_field_value(jira_fields, "assignee") or ""
        out["assignee_identity"] = _identity_of(jira_fields.get("assignee"))
    if "reporter" in jira_fields:
        out["reporter_identity"] = _identity_of(jira_fields.get("reporter"))
    if "parent" in jira_fields:
        parent_raw = jira_fields.get("parent")
        out["remote_parent_id"] = parent_raw.get("key") if isinstance(parent_raw, dict) else None

    # Prefer rebar-status: annotation label over raw Jira workflow status.
    # The label recovers ANY valid local status directly (reverse-map-free).
    local_status: str | None = recover_status_label(jira_fields.get("labels"))
    if local_status is None and "status" in jira_fields:
        # Bug 5886: an unmapped Jira status must NOT default to "open" (that silently
        # reopened closed tickets). Leave it None so the dict omits status → no diff.
        status_raw = _extract_jira_field_value(jira_fields, "status") or "To Do"
        local_status = _JIRA_TO_LOCAL_STATUS.get(status_raw)
    if local_status is not None:
        out["status"] = local_status

    return out


# Distinguish absent, empty, resolved, and unresolved parent observations.
# Only an observed empty parent can support a clear.
PARENT_UNOBSERVED = "unobserved"  # no ``parent`` key: never queried / read failed / truncated
PARENT_NO_PARENT = "no-parent"  # key PRESENT and falsy: queried, Jira has NO parent
PARENT_RESOLVED = "resolved"  # a parent key that resolves to a bound local id
PARENT_UNRESOLVABLE = "unresolvable"  # a parent key we cannot resolve yet (unbound / malformed)


def classify_inbound_parent(
    jira_fields: dict[str, Any],
    get_local_id: Callable[[str], str | None],
) -> tuple[str, str | None]:
    """Return the parent observation state and any bound local parent ID.
    A missing parent key means unobserved, including failed or truncated reads.
    A present empty value means Jira reported no parent. Unreadable or unbound
    parent keys remain unresolved and cannot authorize a clear."""
    if "parent" not in jira_fields:
        return PARENT_UNOBSERVED, None
    parent_raw = jira_fields.get("parent")
    if not parent_raw:
        return PARENT_NO_PARENT, None
    if not isinstance(parent_raw, dict):
        # Jira HAS a parent but not in the ``{"key": ...}`` REST shape we can read. Treat as
        # unresolvable (skip), never as "no parent" — an unreadable value is not evidence of
        # absence.
        return PARENT_UNRESOLVABLE, None
    parent_jira_key = parent_raw.get("key")
    if not parent_jira_key:
        return PARENT_UNRESOLVABLE, None
    local_parent_id = get_local_id(parent_jira_key)
    if local_parent_id is None:
        return PARENT_UNRESOLVABLE, None  # not bound yet — retry next pass
    return PARENT_RESOLVED, local_parent_id


def diff_inbound_parent(
    jira_fields: dict[str, Any],
    local_ticket: dict[str, Any],
    get_local_id: Callable[[str], str | None],
    peer_parent: str | None = None,
) -> tuple[bool, str | None]:
    """Return whether to emit parent_id and its new local value.
    Set a resolved parent only when it differs. Ignore unobserved or unresolved
    parents. Clear an observed empty parent only when the peer previously had one
    and the local reference passes the managed removal gate. A locally managed
    ref alone does not prove Jira ever had that parent. Same-pass outbound
    suppression protects local parent writes, but a degraded outbound pass remains
    a gap."""
    state, jira_parent_local_id = classify_inbound_parent(jira_fields, get_local_id)
    local_parent_id = local_ticket.get("parent_id") or None

    if state == PARENT_RESOLVED:
        if jira_parent_local_id != local_parent_id:
            return True, jira_parent_local_id
        return False, None
    if state != PARENT_NO_PARENT:
        return False, None
    if local_parent_id is None:
        return False, None  # steady state: parentless on both sides, no churn
    if not peer_parent:
        # NO EVIDENCE THE PEER EVER HAD A PARENT -> this is not a deletion. This is the
        # branch whose absence orphaned 63 tickets: see the module docstring.
        return False, None
    from rebar.reducer._managed_refs import should_propagate_removal

    if not should_propagate_removal("parent", local_parent_id, local_ticket):
        return False, None
    return True, None
