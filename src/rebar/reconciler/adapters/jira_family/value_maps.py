"""Define canonical Jira-family value maps and link vocabulary.

Cloud and Data Center share these maps. Callers apply map-or-drift or
map-or-default behavior when project overrides do not resolve a value.
"""

from __future__ import annotations

# Local priority integer (0-4) -> Jira priority name.
LOCAL_PRIORITY_TO_JIRA: dict[int, str] = {
    0: "Highest",
    1: "High",
    2: "Medium",
    3: "Low",
    4: "Lowest",
}

# Syncable local ticket types map to Jira issue types. ``session_log`` remains
# local-only.
LOCAL_TYPE_TO_JIRA: dict[str, str] = {
    "bug": "Bug",
    "story": "Story",
    "task": "Task",
    "epic": "Epic",
}

# Jira Cloud accepts summaries through 254 characters and labels through 255.
# Data Center label limits can differ and require deployment-specific handling.
JIRA_SUMMARY_MAX_CHARS: int = 254
JIRA_LABEL_MAX_CHARS: int = 255

# Map local states to Jira workflow names. Annotation labels preserve states
# collapsed onto a shared Jira status.
LOCAL_STATUS_TO_JIRA: dict[str, str] = {
    "idea": "IDEA",
    "open": "To Do",
    "in_progress": "In Progress",
    "closed": "Done",
    "blocked": "In Progress",
    "cancelled": "Done",
    "deleted": "Done",
}


# Map supported directed relations to Jira link types and record when endpoint
# order reverses. ``UNSYNCED_RELATIONS`` records unsupported vocabulary instead
# of approximating it.
RELATION_TO_JIRA_LINK: dict[str, tuple[str, bool]] = {
    "blocks": ("Blocks", False),
    "depends_on": ("Blocks", True),  # A depends_on B == B blocks A
    "relates_to": ("Relates", False),
}

# Keep unsupported relations local to preserve kind and direction. Together with
# ``RELATION_TO_JIRA_LINK``, this table partitions the relation vocabulary.
UNSYNCED_RELATIONS: dict[str, str] = {
    "duplicates": (
        "stock Jira has a Duplicate link type but the measured DC instance exposes only "
        "Blocks/Relates; unmapped rather than conditionally mapped, so behaviour does not "
        "depend on which link types an instance happens to define"
    ),
    "supersedes": "no stock Jira link type expresses supersession",
    "discovered_from": "rebar provenance concept; no stock Jira analogue",
    "caused_by": "rebar causal concept; no stock Jira analogue",
}
