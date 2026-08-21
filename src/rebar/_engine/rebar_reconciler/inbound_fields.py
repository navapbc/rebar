"""Jira -> local field/status translation for the inbound differ.

This is the self-contained *translation* layer extracted from ``inbound_differ``
(module-size split, epic 716f): the hand-maintained Jira-issuetype / priority /
workflow-status maps and the pure helpers that turn a raw Jira ``fields`` dict
into the local ticket field/value shape the differ then diffs against.

It is a LEAF: every function here references only other symbols in this module
(and the sibling ``adf`` module, loaded lazily by-path, plus the provider-agnostic
managed-ref removal gate that ``diff_inbound_parent`` imports lazily — reused, never
re-implemented). ``inbound_differ`` imports these names back and re-exports them, so
``inbound_differ.<symbol>`` attribute access (and the config parity tests) keep
resolving unchanged.

This module is pure: no I/O, no time/random, no logging, no globals beyond the
lazy ``adf`` module cache.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rebar_reconciler._backend import OutboundMapper

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
    spec.loader.exec_module(mod)
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
    """Normalize the description pair for the inbound compare, or signal echo-unchanged.

    Returns ``None`` when Jira's value is the echo of the LOCAL body's landed wire form —
    the inbound mirror of the outbound ``_baseline_form_matches`` (story 3388/3289). Under
    a lossy one-way rich-text codec (the DC Markdown->wiki cutover) the local body stays
    Markdown while Jira echoes the rendered wiki, so a raw/ADF-only compare reports
    "changed" every pass and the differ pulls Jira's echo of our OWN push back over the
    local Markdown -- the AC2 echo-safety regression. When the injected outbound port is
    present we render the local body to its landed wire form (``map_fields_to_remote`` --
    identity when the rich cutover is off, so the plain-codec path is unchanged) and, if it
    matches the RAW Jira value, treat the field as unchanged. Matching on the landed form
    only ADDS a way to conclude "unchanged": it cannot hide a real Jira edit (a genuine
    remote change no longer equals our landed form) and it cannot start missing an edit the
    raw compare already caught.

    Otherwise returns the ADF-normalized ``(local_val, jira_val)`` pair for the generic
    scalar compare (the Cloud path, byte-for-byte unchanged).
    """
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


def normalize_rich_text(body: Any) -> str:
    """Decode a rich-text payload (ADF dict or string) to plain text (ticket 21ca).

    Jira: an ADF dict decodes via ``adf_to_text``; a plain string passes through
    unchanged; ``None`` yields ``""``. This is the ``InboundMapper.normalize_rich_text``
    Backend-port implementation (``adapters/jira/backend.py:_JiraInbound``); it serves
    BOTH the inbound apply defense-in-depth (``inbound_translate._normalize_adf_body``)
    and the outbound comment-diff decode (``outbound_comments._normalize_comment_body``).
    """
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
    """Map Jira fields to local ticket field names/values (partial-tolerant).

    ticket 929a: when jira_fields carries a rebar-status: annotation label
    (e.g. ``rebar-status:blocked``), the label takes precedence over the raw
    Jira workflow status for the local status mapping. This preserves lossless
    round-trip for statuses that have no direct Jira equivalent (blocked maps
    to In Progress on Jira, cancelled maps to Done). Without this, a
    blocked→In Progress outbound followed by an inbound pass would silently
    overwrite local "blocked" with "in_progress".

    ticket 625b: every field's emission is guarded on its SOURCE vendor key being
    PRESENT in ``jira_fields`` (``if key in jira_fields``), so a partial subset
    (e.g. the ``_BASELINE_FIELDS`` slice stored at rest) maps only the keys it
    carries while a FULL snapshot maps byte-identically to today (a present-but-null
    field still maps to its former default). An ABSENT vendor key yields NO canonical
    key — never a fabricated default. Three canonical keys are added so the core
    never reads a raw Jira snapshot shape: ``assignee_identity`` /
    ``reporter_identity`` (fixed ``{display,email,account_id}`` shape) and the bare
    ``remote_parent_id`` string. The scalar ``assignee`` string is kept alongside
    ``assignee_identity`` (additive — existing consumers).
    """
    out: dict[str, Any] = {}

    if "summary" in jira_fields:
        out["title"] = _extract_jira_field_value(jira_fields, "summary") or ""
    if "description" in jira_fields:
        # Bug 1bb2: ``_extract_jira_field_value`` returns nested dicts verbatim
        # for any field that isn't a {.name/.displayName} object — Jira's
        # ``description`` is an ADF (Atlassian Document Format) dict in cloud
        # tenants. Normalize to plain text here so the diff map carries a
        # string and the applier writes a string into the local EDIT event.
        description_raw = jira_fields.get("description")
        out["description"] = _normalize_jira_body(description_raw) if description_raw else ""
    if "issuetype" in jira_fields:
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


# ---------------------------------------------------------------------------
# Inbound parent: the THREE-state read and the gated clear (ticket 88d9)
# ---------------------------------------------------------------------------

# The four situations the snapshot's ``parent`` field can be in. Before 88d9 the
# differ collapsed all of them into "resolved to a local id, or not", which made a
# de-parenting in Jira indistinguishable from a read that never happened — so the
# differ could not clear a parent without risking a spurious clear, and therefore
# never cleared one at all.
PARENT_UNOBSERVED = "unobserved"  # no ``parent`` key: never queried / read failed / truncated
PARENT_NO_PARENT = "no-parent"  # key PRESENT and falsy: queried, Jira has NO parent
PARENT_RESOLVED = "resolved"  # a parent key that resolves to a bound local id
PARENT_UNRESOLVABLE = "unresolvable"  # a parent key we cannot resolve yet (unbound / malformed)


def classify_inbound_parent(
    jira_fields: dict[str, Any],
    get_local_id: Callable[[str], str | None],
) -> tuple[str, str | None]:
    """Classify a snapshot entry's ``parent`` field; return ``(state, local_parent_id)``.

    ``local_parent_id`` is non-None only for :data:`PARENT_RESOLVED`.

    The distinction that matters is PRESENCE, not truthiness: ``fetcher.merge_parent_map``
    writes the key only for issues the parent map actually mentioned, so a missing key means
    "we did not observe this issue" (a truncated page walk, a cross-project issue, or the
    whole-map ``{}`` degradation ``get_parent_map`` returns on ANY REST failure) while a
    present-and-falsy value means "Jira was asked and answered: no parent". Only the latter
    may authorise a clear — this is the same predicate shape (``key in m and not m[key]``)
    that story 5200's parent wait-helper had to be fixed to before landing.
    """
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
    """Decide the inbound ``parent_id`` change for one bound ticket (ticket 88d9).

    Returns ``(emit, value)``. ``emit`` False means the differ writes nothing for this field.

    An inbound CLEAR is a WRITE THAT DESTROYS LOCAL DATA, so it fires only on POSITIVE
    evidence — never on absent snapshot data — and only through the shared managed-ref gate:

      * :data:`PARENT_RESOLVED`     -> set/compare as before (a SET is local-authoritative
                                       once observed, and unchanged emits nothing).
      * :data:`PARENT_UNOBSERVED`   -> nothing. The read may have failed or been truncated.
      * :data:`PARENT_UNRESOLVABLE` -> nothing, preserving the original guard's intent: "we do
                                       NOT emit parent_id=None to avoid accidentally clearing a
                                       locally-set parent when we just can't resolve it yet".
      * :data:`PARENT_NO_PARENT`    -> the CLEAR, gated on ``should_propagate_removal`` so a
                                       parent a human set directly in Jira (one rebar never
                                       managed) is ADOPTED rather than clobbered. This is the
                                       SAME provider-agnostic predicate the outbound direction
                                       wraps as ``_parent_clear_is_managed``; reused, not
                                       re-invented.

    KNOWN BLIND SPOT, recorded not hidden: ``add_managed_ref`` is folded by the parent-set
    EVENT, so a ref is "managed" the instant it is set LOCALLY — MANAGED does not prove Jira
    ever had the parent. The differ's same-pass suppression closes that (an inbound field the
    same pass's outbound is writing is dropped, and ``_OUTBOUND_TO_INBOUND_FIELD`` maps
    ``parent`` -> ``parent_id`` so it genuinely fires here). The residual window, stated
    plainly: a degraded pass in which outbound does not write the parent (skipped, disabled, or
    failed) while inbound proceeds.
    """
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
