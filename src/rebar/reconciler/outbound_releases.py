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
from rebar.reconciler.release_alerts import ARCHIVED, project_of


def _diff_releases(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    intent_set: set[str] | None = None,
) -> list[dict[str, Any]]:
    """The Jira edits for one bound issue with availability unknown: ``{"action":
    "add"|"remove", "release": name}`` records, ``[]`` when ``fixVersions`` is unobserved."""
    result = _split_releases(ticket, jira_fields, intent_set, None)
    return [] if result is None else result[0]


def _split_releases(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    intent_set: set[str] | None = None,
    availability: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]] | None:
    """Return ``(mutations, withheld)`` for one bound issue, or None when unobserved.

    ``mutations`` are ``{"action": "add"|"remove", "release": name}`` records. An
    unobserved (absent or non-list) ``fixVersions`` returns None: a snapshot that never
    fetched the field cannot be read as "Jira has no versions". When ``intent_set`` is
    None every difference is emitted (direct callers and tests).

    ``availability`` is the project's ``{name: "available"|"archived"}`` map, or None
    when unknown (prior behavior: adds are attempted). With a known map, an intended ADD
    of a missing or archived name becomes a ``{"release", "reason"}`` withheld record
    instead of a Jira add (ADR 0124 decisions 8, 9); REMOVEs are unaffected.
    """
    fix_versions = jira_fields.get("fixVersions")
    if not isinstance(fix_versions, list):
        return None
    jira_names = _jira_release_names(fix_versions)
    local_names = {r for r in ticket.get("releases") or [] if isinstance(r, str) and r}

    def _intended(name: str) -> bool:
        return intent_set is None or name in intent_set

    mutations: list[dict[str, Any]] = []
    withheld: list[dict[str, str]] = []
    for name in sorted(local_names - jira_names):
        if not _intended(name):
            continue
        status = None if availability is None else availability.get(name, "missing")
        if status in ("missing", ARCHIVED):
            withheld.append({"release": name, "reason": status})
        else:
            mutations.append({"action": "add", "release": name})
    mutations.extend(
        {"action": "remove", "release": name}
        for name in sorted(jira_names - local_names)
        if _intended(name)
    )
    return mutations, withheld


def release_mutations_for(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    local_id: str,
    jira_key: str,
    config: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Diff one bound ticket; return ``(mutations, withheld names)``.

    A ticket with no intent entry gets an empty intent set (lazy first-pass safety: no
    edits without event-log evidence). When the project's availability is known and the
    snapshot observed ``fixVersions``, the ticket's withheld records go to
    ``config.release_withheld_sink`` so the alert lifecycle can open and resolve alerts.
    """
    intent_map = config.local_release_intent
    intent_set = None if intent_map is None else intent_map.get(local_id, set())
    known = config.release_availability or {}
    availability = known.get(project_of(jira_key)) if jira_key else None
    result = _split_releases(ticket, jira_fields, intent_set, availability)
    if result is None:
        return [], []
    mutations, withheld = result
    sink = config.release_withheld_sink
    if availability is not None and sink is not None:
        sink[local_id] = (jira_key, withheld)
    return mutations, [w["release"] for w in withheld]
