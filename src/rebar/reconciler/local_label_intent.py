"""Local label-intent helper for the outbound differ (bug a06c).

Scans a bound ticket's event log under the tracker directory  # tickets-boundary-ok
and returns the union of every label that ever appeared in a CREATE,
EDIT (``data.fields.tags``), TAG_DELTA (``data.added``, P2.3), or SNAPSHOT
(``data.compiled_state.tags``) event. That "ever-seen" set is consumed by the
outbound differ to gate
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
from collections.abc import Callable
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
    # P2.3: TAG_DELTA (value of rebar.reducer._version.TAG_DELTA). User-added tags
    # now arrive as deltas, so they must contribute to the ever-seen intent set.
    # Skip inbound-origin deltas FIRST (same rationale as the EDIT branch: they are
    # Jira mutations applied locally, not user intent), then fold ONLY ``data.added``
    # — NEVER ``data.removed``: this is an ever-seen UNION, so subtracting removals
    # would resurrect the a06c spurious-REMOVE bug.
    if event_type == "TAG_DELTA":
        if data.get("source") == "inbound":
            return None
        added = data.get("added")
        if isinstance(added, list):
            return [str(t) for t in added]
        return None
    return None


def _ever_seen(
    ticket_id: str, tracker_dir: Path, extractor: Callable[[dict[str, Any]], list[str] | None]
) -> set[str]:
    """Union of every name ``extractor`` returns across the ticket's event files.

    Missing ticket directory -> empty set (lazy first pass: the caller treats this as
    "suppress all REMOVEs", a safe degradation). Malformed or unreadable event files
    are skipped so a single corrupt file never aborts the pass.
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
        names = extractor(event)
        if names:
            ever_seen.update(names)
    return ever_seen


def compute_label_intent_set(ticket_id: str, tracker_dir: Path) -> set[str]:
    """Read the ticket's event directory and return the ever-seen tag set.

    Args:
        ticket_id: ticket directory name (e.g., ``"a06c-7075-6c1e-4a96"``).
        tracker_dir: path to ``.tickets-tracker`` (the orphan-branch worktree).

    Returns:
        Union of every tag that appeared in a CREATE / EDIT (with
        ``data.fields.tags``) / TAG_DELTA / SNAPSHOT event in this ticket's
        directory; empty when the directory is missing or has no contributing
        events (see ``_ever_seen`` for the failure modes).
    """
    return _ever_seen(ticket_id, tracker_dir, _extract_tags_from_event)


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


def _extract_releases_from_event(event: dict[str, Any]) -> list[str] | None:
    """Release names one event contributes to the ever-seen set (ADR 0124 decision 5).

    Non-inbound ``RELEASE_DELTA.added`` and ``SNAPSHOT.compiled_state.releases``.
    Inbound-origin deltas are Jira changes applied locally, not user intent, and
    ``removed`` is never folded: this is an ever-seen UNION, as for tags (bug a06c).
    """
    event_type = event.get("event_type")
    data = event.get("data") or {}
    if event_type == "RELEASE_DELTA":
        if data.get("source") == "inbound":
            return None
        added = data.get("added")
        return [str(n) for n in added] if isinstance(added, list) else None
    if event_type == "SNAPSHOT":
        releases = (data.get("compiled_state") or {}).get("releases")
        return [str(n) for n in releases] if isinstance(releases, list) else None
    return None


def compute_release_intent_set(ticket_id: str, tracker_dir: Path) -> set[str]:
    """The ever-seen release-name set for one ticket (gates outbound fixVersions edits)."""
    return _ever_seen(ticket_id, tracker_dir, _extract_releases_from_event)


def compute_release_intent_map(
    bound_local_ids: list[str], tracker_dir: Path
) -> dict[str, set[str]]:
    """Bulk helper: the release intent set for every bound ticket, once per pass."""
    return {
        local_id: compute_release_intent_set(local_id, tracker_dir) for local_id in bound_local_ids
    }
