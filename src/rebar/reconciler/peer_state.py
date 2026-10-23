"""Last-synced field and parent evidence stored with each binding.

The 5-field baseline is the three-way-merge ancestor; its absence is valid and
falls back to local wins. The last observed peer parent supports inbound clears;
missing observation means no clear. Free functions mutate binding records in
memory, while ``BindingStore.save()`` retains the existing persistence path and
delegating API (ADR 0026).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from rebar.reconciler.inbound_fields import normalize_baseline_value

# ADR 0026: the five last-synced Jira-side inbound-mirrored fields per binding.
# An absent baseline (including v1) is valid and degrades to local-wins.
_BASELINE_FIELDS: tuple[str, ...] = (
    "summary",
    "description",
    "priority",
    "status",
    "assignee",
)


def get_baseline(bindings: dict[str, Any], local_id: str) -> dict[str, Any] | None:
    """Return a copy of last-synced fields, or ``None`` for local-wins fallback."""
    entry = bindings.get(local_id)
    if entry is None:
        return None
    baseline = entry.get("baseline")
    if not isinstance(baseline, dict):
        return None
    return dict(baseline)


def set_baseline(bindings: dict[str, Any], local_id: str, fields: dict[str, Any]) -> None:
    """Set normalized mirrored fields for a bound ID; persist on store save."""
    entry = bindings.get(local_id)
    if entry is None:
        return
    baseline = {k: normalize_baseline_value(k, fields[k]) for k in _BASELINE_FIELDS if k in fields}
    if entry.get("baseline") == baseline:
        return
    previous = entry.get("baseline") or {}
    entry["baseline"] = baseline
    _clear_rich_reemit_on_body_refresh(entry, previous, baseline)


def merge_baseline(bindings: dict[str, Any], local_id: str, fields: dict[str, Any]) -> None:
    """Merge only confirmed outbound writes into the last-synced baseline.

    The pass-start fetch predates outbound apply. Leaving its old values for
    fields just pushed can misclassify a local revert on the next pass. The
    caller must supply per-mutation successes only; advancing for a failed
    write would assert synchronization that never happened. Untouched fields
    retain fetched values, and an unchanged merge causes no timestamp churn.
    """
    entry = bindings.get(local_id)
    if entry is None:
        return
    overlay = {k: normalize_baseline_value(k, fields[k]) for k in _BASELINE_FIELDS if k in fields}
    if not overlay:
        return
    baseline = dict(entry.get("baseline") or {})
    merged = {**baseline, **overlay}
    if merged == baseline:
        return
    entry["baseline"] = merged
    _clear_rich_reemit_on_body_refresh(entry, baseline, merged)


# -- rich-text emit state (story 3388) ---------------------------------------
#
# ``rich_sha`` identifies the last sent wire; ``rich_reemit`` bounds repeated
# sends when the DC codec has no fixed point. Both change only on confirmed
# description writes; absent ``rich_reemit`` means zero.

RICH_REEMIT_OBSERVE_AT = 2
"""Re-emit count at which the caller should OBSERVE the body Jira actually stored.

Two means the same wire has been pushed three times (0, 1, 2) without the
baseline moving underneath it — no longer explicable as one pass of lag, so the
next step is evidence rather than another blind re-push. It is an equality
threshold, not a floor, so the observation costs exactly one GET per divergence
episode however long the episode lasts.
"""


def rich_sha(wire: Any) -> str:
    """Return a 16-hex change gate for string or sorted-key object wires.

    The short digest limits binding size; it is not an authenticator.
    """
    payload = wire if isinstance(wire, str) else json.dumps(wire, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def note_rich_emit(entry: dict[str, Any], wire: Any) -> int:
    """Track a confirmed description push on the live binding entry.

    Return 0 for a new wire, then count consecutive repeats. A changed wire
    resets the count; a converged body never stores ``rich_reemit``. The store's
    existing save path persists the in-place entry change.
    """
    sha = rich_sha(wire)
    if entry.get("rich_sha") != sha:
        entry["rich_sha"] = sha
        entry.pop("rich_reemit", None)
        return 0
    count = int(entry.get("rich_reemit") or 0) + 1
    entry["rich_reemit"] = count
    return count


def _clear_rich_reemit_on_body_refresh(
    entry: dict[str, Any], previous: dict[str, Any], updated: dict[str, Any]
) -> None:
    """Reset repeat count only when fresh baseline evidence changes the body.

    Other field changes say nothing about description convergence. ``rich_sha``
    still identifies the last wire sent.
    """
    if previous.get("description") != updated.get("description"):
        entry.pop("rich_reemit", None)


def get_peer_parent(bindings: dict[str, Any], local_id: str) -> str | None:
    """Return the last observed peer parent, or ``None`` to suppress a clear.

    Local ``managed_refs`` cannot prove the peer ever carried that parent.
    """
    entry = bindings.get(local_id)
    if entry is None:
        return None
    value = entry.get("peer_parent")
    return value if isinstance(value, str) and value else None


def set_peer_parent(bindings: dict[str, Any], local_id: str, parent_key: str | None) -> None:
    """Record an observed peer parent, with ``None`` meaning observed empty.

    Call only when the snapshot carried the parent field. A partial or failed
    read must not turn missing data into deletion evidence. Unbound IDs are
    ignored; the binding store persists changes on save.
    """
    entry = bindings.get(local_id)
    if entry is None:
        return
    normalized = parent_key if isinstance(parent_key, str) and parent_key else ""
    if entry.get("peer_parent", None) == normalized:
        return
    entry["peer_parent"] = normalized


def seed_baselines_from_snapshot(bindings: dict[str, Any], prev_snapshot: dict[str, Any]) -> int:
    """Seed baselines for bound Jira keys in a snapshot; return the count.

    This leaves the snapshot and its other consumers intact.
    """
    seeded = 0
    for local_id, entry in bindings.items():
        jira_key = entry.get("jira_key")
        if jira_key and jira_key in prev_snapshot:
            set_baseline(bindings, local_id, prev_snapshot[jira_key])
            seeded += 1
    return seeded
