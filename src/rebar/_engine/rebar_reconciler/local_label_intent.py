"""Local label-intent helper for the outbound differ (bug a06c).

Scans a bound ticket's event log under the tracker directory  # tickets-boundary-ok
and returns the union of every label that ever appeared in a CREATE,
EDIT (``data.fields.tags``), or SNAPSHOT (``data.compiled_state.tags``)
event. That "ever-seen" set is consumed by the outbound differ to gate
label REMOVE emission — a Jira-only label only counts as a legitimate
local-removal-intent if it appears in the ever-seen set.

Without this gating, the outbound differ emits a spurious REMOVE for
every Jira-side label that local never had, which (combined with the
PR #457 local-wins bidir suppression) silently drops legitimate
inbound label ADDs. See decision recorded on bug a06c-7075-6c1e-4a96.

This module owns the I/O the outbound_differ.py contract refuses to do
(``outbound_differ.py`` is pure-no-I/O by design).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _extract_tags_from_event(event: dict[str, Any]) -> list[str] | None:
    """Return the tags list contributed by a single event, or None.

    Event-type contributions:
      - CREATE    -> ``data.tags``
      - EDIT      -> ``data.fields.tags`` (when present)
      - SNAPSHOT  -> ``data.compiled_state.tags``

    All other event types and EDIT events that do not touch tags
    contribute nothing.
    """
    event_type = event.get("event_type")
    data = event.get("data") or {}
    if event_type == "CREATE":
        tags = data.get("tags")
        if isinstance(tags, list):
            return [str(t) for t in tags]
        return None
    if event_type == "EDIT":
        # Bug a06c: skip inbound-origin EDITs. They reflect Jira side
        # mutations the reconciler applied locally, not user intent.
        # Counting them as intent would re-add labels Jira just removed
        # (T4 IB-REMOVE regression). The applier writes
        # ``data.source = "inbound"`` on every inbound labels EDIT.
        if data.get("source") == "inbound":
            return None
        fields = data.get("fields") or {}
        if "tags" not in fields:
            return None
        tags = fields.get("tags")
        if isinstance(tags, list):
            return [str(t) for t in tags]
        if isinstance(tags, str):
            return [t.strip() for t in tags.split(",") if t.strip()]
        return []
    if event_type == "SNAPSHOT":
        compiled = data.get("compiled_state") or {}
        tags = compiled.get("tags")
        if isinstance(tags, list):
            return [str(t) for t in tags]
        return None
    return None


def compute_label_intent_set(ticket_id: str, tracker_dir: Path) -> set[str]:
    """Read the ticket's event directory and return the ever-seen tag set.

    Args:
        ticket_id: ticket directory name (e.g., ``"a06c-7075-6c1e-4a96"``).
        tracker_dir: path to ``.tickets-tracker`` (the orphan-branch worktree).

    Returns:
        Union of every tag that appeared in a CREATE / EDIT (with
        ``data.fields.tags``) / SNAPSHOT event in this ticket's directory.
        Empty set when the directory is missing or contains no
        contributing events.

    Failure modes:
        - Missing ticket directory -> empty set (lazy first-pass: caller
          treats this as "suppress all REMOVEs", safe degradation).
        - Malformed event JSON -> skip that event, continue with the rest.
          A single corrupt file must not abort the pass.
        - Unreadable file -> skip silently (same rationale).
    """
    ticket_dir = Path(tracker_dir) / ticket_id
    if not ticket_dir.is_dir():
        return set()

    ever_seen: set[str] = set()
    for entry in sorted(ticket_dir.iterdir()):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        try:
            with open(entry, encoding="utf-8") as f:
                event = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(event, dict):
            continue
        tags = _extract_tags_from_event(event)
        if tags:
            ever_seen.update(tags)
    return ever_seen


def compute_label_intent_map(bound_local_ids: list[str], tracker_dir: Path) -> dict[str, set[str]]:
    """Bulk helper: compute the intent set for every bound ticket.

    Called once per reconcile pass by ``reconcile.py`` before invoking
    the outbound differ. Iterates ``bound_local_ids`` and returns a
    dict mapping each to its ever-seen tag set. Tickets whose directory
    is missing contribute an empty set (preserves the lazy first-pass
    safety property at the map level).
    """
    return {
        local_id: compute_label_intent_set(local_id, tracker_dir) for local_id in bound_local_ids
    }
