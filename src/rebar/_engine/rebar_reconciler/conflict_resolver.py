"""Conflict resolver for rebar_reconciler.

Provides a FIELD_CLASSES registry and three resolution strategies:
  - resolve_state: local always wins (for deterministic single-value fields)
  - resolve_additive: merge content (lists union-ordered, strings concat, None-safe)
  - resolve_set_valued: union of both sets

resolve_field dispatches to the correct strategy based on FIELD_CLASSES.

Also exposes ProvenanceLedger — per-element provenance keyed by `element_key`
(typically `"<field_name>:<element_value>"` for collection elements, or just
`<field_name>` for scalars). The ledger uses element-level keys so collection
elements can be echo-suppressed individually; it is consumed by differ's
`compute_mutations(..., ledger=)` echo-suppression path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Field class registry
# ---------------------------------------------------------------------------

FIELD_CLASSES: dict[str, str] = {
    "status": "state",
    "assignee": "state",
    "priority": "state",
    "title": "state",
    "type": "state",
    "description": "additive",
    "comments": "additive",
    "labels": "set",
    "watchers": "set",
    "links": "set",
}


# ---------------------------------------------------------------------------
# Resolution strategies
# ---------------------------------------------------------------------------


def resolve_state(local_val: Any, remote_val: Any) -> Any:
    """Local always wins for state fields."""
    return local_val


def resolve_additive(local_val: Any, remote_val: Any) -> Any:
    """Merge additive fields.

    - Both lists  → union ordered (preserve order, no duplicates).
    - Both strings → append remote if it adds new content.
    - One or both None → return whichever is non-None (local preferred).
    """
    # Both lists: union ordered, local first
    if isinstance(local_val, list) and isinstance(remote_val, list):
        seen: set[str] = set()
        result: list[Any] = []
        for item in local_val + remote_val:
            key = str(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    # Both strings: append remote only when it contributes new content
    if isinstance(local_val, str) and isinstance(remote_val, str):
        if remote_val and remote_val not in local_val:
            return (local_val + "\n" + remote_val) if local_val else remote_val
        return local_val

    # One or both are None — return whichever is non-None (local first)
    return local_val if local_val is not None else remote_val


_PROVENANCE_CAP = 50


def resolve_set_valued(
    local_set: Any,
    remote_set: Any,
    provenance_record: Optional[Any],
) -> list[Any]:
    """Union of both sets; updates provenance_record with a FIFO cap of 50."""
    seen: set[Any] = set()
    merged: list[Any] = []
    local_list = list(local_set) if local_set else []
    remote_list = list(remote_set) if remote_set else []
    for item in local_list + remote_list:
        if item not in seen:
            merged.append(item)
            seen.add(item)

    if provenance_record is not None and isinstance(provenance_record, list):
        for item in merged:
            if item not in provenance_record:
                provenance_record.append(item)
        # Enforce bounded *size*, not bounded growth. If provenance_record was
        # already over cap when passed in (schema migration, alternate writer),
        # pop(0)+append() would leave the length unchanged. Truncate from the
        # front so the cap is restored regardless of input size.
        while len(provenance_record) > _PROVENANCE_CAP:
            provenance_record.pop(0)

    return merged


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _element_id(field_name: str, element: Any) -> str:
    """Pin a stable per-element identifier for ledger keys.

    Per the AC amendment on task 902e-1eea:
      - comments: Jira id when present, else sha256(body) as surrogate
      - labels: the label string itself
      - watchers: account id (preferred), else display name
      - links: composite f"{link_type}:{target_key}"
      - default: str(element)
    """
    import hashlib

    if isinstance(element, dict):
        if field_name == "comments":
            if "id" in element and element["id"]:
                return str(element["id"])
            body = element.get("body", "")
            return hashlib.sha256(str(body).encode("utf-8")).hexdigest()
        if field_name == "watchers":
            return str(element.get("accountId") or element.get("displayName") or element)
        if field_name == "links":
            link_type = element.get("type", "")
            target = element.get("target") or element.get("target_key", "")
            return f"{link_type}:{target}"
    return str(element)


def resolve_field(
    field_name: str,
    local_val: Any,
    remote_val: Any,
    provenance_record: Optional[Any] = None,
    *,
    ledger: Optional[Any] = None,
) -> Any:
    """Dispatch to the correct resolver based on FIELD_CLASSES.

    Unknown field names default to resolve_state (local wins).

    When `ledger` is provided (a ProvenanceLedger-shaped object exposing
    `record(key, side, value)`), each resolved element is recorded:
      - scalar (state/additive non-list): one record under key=field_name
      - list/set collections: one record per element under
        key=f"{field_name}:{element_id}", with side determined by which
        side contributed that element (local-first when present in both).

    When `ledger` is None, behavior is identical to the pre-ledger
    contract — no ledger calls, no exceptions.
    """
    field_class = FIELD_CLASSES.get(field_name, "state")

    if field_class == "state":
        resolved = resolve_state(local_val, remote_val)
        if ledger is not None:
            ledger.record(field_name, "local", resolved)
        return resolved

    if field_class == "additive":
        resolved = resolve_additive(local_val, remote_val)
        if ledger is not None:
            # Lists → one record per element; non-list (string/None) → one record per call.
            if isinstance(resolved, list):
                local_items = list(local_val) if isinstance(local_val, list) else []
                local_keys = {_element_id(field_name, item) for item in local_items}
                for item in resolved:
                    eid = _element_id(field_name, item)
                    side = "local" if eid in local_keys else "jira"
                    ledger.record(f"{field_name}:{eid}", side, item)
            else:
                # Scalar additive (e.g., description string) — local-first attribution.
                side = "local" if local_val else "jira"
                ledger.record(field_name, side, resolved)
        return resolved

    if field_class == "set":
        resolved = resolve_set_valued(local_val, remote_val, provenance_record)
        if ledger is not None:
            local_items = list(local_val) if local_val else []
            local_keys = {_element_id(field_name, item) for item in local_items}
            for item in resolved:
                eid = _element_id(field_name, item)
                side = "local" if eid in local_keys else "jira"
                ledger.record(f"{field_name}:{eid}", side, item)
        return resolved

    # Fallback (should not be reached with current registry values)
    resolved = resolve_state(local_val, remote_val)
    if ledger is not None:
        ledger.record(field_name, "local", resolved)
    return resolved


# ---------------------------------------------------------------------------
# ProvenanceLedger — element-level provenance for echo suppression
# ---------------------------------------------------------------------------


def _hash_value(value: Any) -> str:
    """Stable sha256 of a JSON-serialized value."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ProvenanceLedger:
    """Element-level provenance ledger.

    Records per-element writes keyed by `element_key` (typically
    `"<field_name>:<element_value>"` for collection elements, or `<field_name>`
    for scalars). Each record carries `side` ('local'|'jira'), an ISO 8601
    UTC `timestamp`, and a `value_hash` for content-equality echo detection.

    The ledger uses element-level keys so individual collection elements can be
    echo-suppressed independently (vs whole target+payload keys).
    """

    def __init__(self) -> None:
        self._records: dict[str, list[dict[str, Any]]] = {}

    def record(self, element_key: str, side: Optional[str] = None, value: Any = None) -> None:
        """Append an entry for `element_key`.

        Accepts both positional (`record(key, side, value)`) and keyword
        (`record(element_key=k, side=s, value=v)`) calling styles so existing
        resolver call sites and the matrix tests both work.
        `side` must be 'local' or 'jira' — None is rejected with a clear
        ValueError to flag callers that forgot to pass it.
        """
        if side is None:
            raise ValueError(
                "side is required and must be 'local' or 'jira'; "
                "passing None (or omitting the argument) is a caller bug"
            )
        if side not in ("local", "jira"):
            raise ValueError(f"side must be 'local' or 'jira', got {side!r}")
        entry = {
            "side": side,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "value_hash": _hash_value(value),
        }
        self._records.setdefault(element_key, []).append(entry)

    def is_echo(self, element_key: str, value: Any) -> bool:
        """True iff the MOST RECENT record for `element_key` has a matching content hash.

        Echo semantics are "the value just bounced back to its prior state" —
        compare against the immediate predecessor only, not the entire history.
        A prior version returned True if any historical record matched, which
        treated long-ago values as live echoes and suppressed legitimate
        round-trip mutations (e.g. A → B → C → A: the final A is NOT an echo
        of C, even though A appears earlier in the ledger).
        """
        entries = self._records.get(element_key, [])
        if not entries:
            return False
        return entries[-1]["value_hash"] == _hash_value(value)

    def serialize(self) -> dict[str, dict[str, Any]]:
        """Return the most-recent record per element_key as a flat dict."""
        return {k: entries[-1] for k, entries in self._records.items() if entries}
