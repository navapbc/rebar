"""Jira-family local<->Jira value maps + link vocabulary (story J2, epic e369).

Relocated from ``adapters/jira/jira_fields.py``, and DE-DUPLICATED against the
second copy that used to live in the location-pinned ``adapters/jira/
outbound_fields.py`` (``_LOCAL_TO_JIRA_STATUS`` / ``_LOCAL_TO_JIRA_PRIORITY``). Both
pre-move copies had already drifted; this is now the SOLE definition site under
``adapters/`` for each map (``rebar_reconciler/config.py``'s separate third copy is
deliberately out of this story's scope — see bug fe15-3bc4-ed70-4b61).

The drift: the ``outbound_fields`` copy mapped ``"deleted": "Done"``; the
``jira_fields`` ACLI-side copy omitted the key (falling through to
``status.replace("_", " ").title()`` -> the invalid ``"Deleted"``, which is not a
state in the live DIG workflow). The unified map below adopts ``"deleted": "Done"``
— the value two of the three pre-existing copies already agreed on. This is J2's
ONE deliberate observable behaviour change; see the story description for the full
rationale.
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

# Local ticket type -> Jira issue type name (story bd9e, epic 3e73).
# De-duplicated against the two pre-existing copies read by the two CREATE paths
# (``adapters/jira/outbound_fields.py`` for Cloud, ``adapters/jira_datacenter/
# backend.py`` for DC). Both copies were diffed before the move and were
# CONTENT-IDENTICAL — same four keys, same four values, differing only in literal
# key order, which a dict does not carry semantically — so unlike J2's status map
# (``"deleted"``) this unification is behaviour-preserving on BOTH deployments and
# required no divergence decision. ``"session_log"`` is deliberately ABSENT: the
# type is local-only and must never be created in Jira (pinned by
# ``tests/.../diffing/test_outbound_differ_session_log_exclusion.py``).
LOCAL_TYPE_TO_JIRA: dict[str, str] = {
    "bug": "Bug",
    "story": "Story",
    "task": "Task",
    "epic": "Epic",
}

# Jira hard limits we defend against (verified against Jira Cloud REST API 2026).
# Note the deliberate off-by-one divergence between the two constants:
#   - Summary: Jira's error is "Summary must be less than 255 characters"
#     (strict less-than), so the INCLUSIVE max is 254. A 255-char title is
#     REJECTED. Sources: Atlassian Community thread 989632 + GitHub
#     tenable/integration-jira-cloud issue #322 + GitHub-prior-art audit
#     (2026-05-24, run a52143da).
#   - Label: Jira's error is "Labels can't have spaces or be more than 255
#     characters" (not-more-than), so the INCLUSIVE max is 255. Source:
#     Forge custom-field community thread 55277.
JIRA_SUMMARY_MAX_CHARS: int = 254
JIRA_LABEL_MAX_CHARS: int = 255

# Local status string -> Jira workflow state name.
# status.capitalize() produces "In_progress" for snake_case inputs; this mapping
# ensures correct Jira state names are used in ACLI transition commands.
# ticket 929a: blocked/cancelled map to the nearest live DIG workflow state
# ({To Do, In Progress, In Review, Done} only); lossless information is
# preserved via rebar-status: annotation labels managed by outbound_differ.
LOCAL_STATUS_TO_JIRA: dict[str, str] = {
    "idea": "IDEA",
    "open": "To Do",
    "in_progress": "In Progress",
    "closed": "Done",
    "blocked": "In Progress",
    "cancelled": "Done",
    "deleted": "Done",
}


# rebar relation -> (Jira link type, swap_endpoints). This is Jira-specific link
# vocabulary, single-sourced here in the Jira-family shared layer (ticket 4af8
# relocated it out of the backend-neutral ``outbound_links`` core; J2 relocated it
# again out of Cloud's ``adapters/jira/`` into the shared layer). ``swap_endpoints``
# records that "A relation B" maps to a Jira link with the endpoints reversed:
# "A depends_on B" == "B blocks A". Relations with no reliable Jira link type
# (duplicates / supersedes / discovered_from) are intentionally ABSENT and SKIPPED
# by the differ.
RELATION_TO_JIRA_LINK: dict[str, tuple[str, bool]] = {
    "blocks": ("Blocks", False),
    "depends_on": ("Blocks", True),  # A depends_on B == B blocks A
    "relates_to": ("Relates", False),
}
