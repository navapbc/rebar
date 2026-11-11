"""Outbound rebar ``releases`` -> Jira ``fixVersions`` diff (ADR 0124, decisions 5 and 6).

Mirrors ``outbound_labels._diff_labels``: both an ADD and a REMOVE are gated on the
ticket's ever-seen release intent set (``local_label_intent.compute_release_intent_map``),
so a name that only ever arrived from Jira is never re-added after a Jira-side removal
and a Jira version rebar never held is never removed (bug a06c). The bridge only names
versions that already exist in the Jira project; it never creates one.

Pure: no I/O, like the rest of the outbound differ.
"""

from __future__ import annotations

from typing import Any

from rebar.reconciler.inbound_collection_diffs import _jira_release_names


def _diff_releases(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    intent_set: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return ``{"action": "add"|"remove", "release": name}`` records for one bound issue.

    An unobserved (absent or non-list) ``fixVersions`` yields ``[]``: a snapshot that
    never fetched the field cannot be read as "Jira has no versions". When
    ``intent_set`` is None every difference is emitted (direct callers and tests).
    """
    fix_versions = jira_fields.get("fixVersions")
    if not isinstance(fix_versions, list):
        return []
    jira_names = _jira_release_names(fix_versions)
    local_names = {r for r in ticket.get("releases") or [] if isinstance(r, str) and r}

    def _intended(name: str) -> bool:
        return intent_set is None or name in intent_set

    mutations: list[dict[str, Any]] = [
        {"action": "add", "release": name}
        for name in sorted(local_names - jira_names)
        if _intended(name)
    ]
    mutations.extend(
        {"action": "remove", "release": name}
        for name in sorted(jira_names - local_names)
        if _intended(name)
    )
    return mutations


def release_mutations_for(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    local_id: str,
    local_release_intent: dict[str, set[str]] | None,
) -> list[dict[str, Any]]:
    """Diff one bound ticket with its intent set; a ticket with no intent entry gets an
    empty set (lazy first-pass safety: no edits without event-log evidence)."""
    intent_set = None if local_release_intent is None else local_release_intent.get(local_id, set())
    return _diff_releases(ticket, jira_fields, intent_set)
