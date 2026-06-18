"""Provider-neutral external-tracker stripping (P1.2 ``export --strip-external``).

This is the ONLY seam the GitHub-sync follow-on inherits: it strips *all*
external-tracker linkage from a ticket-state projection, regardless of provider,
so a stripped export carries no foreign-tracker association and re-imports cleanly
into a new project. It deliberately knows nothing about the reconciler — it
pattern-matches the linkage the event-sourced state surfaces:

* top-level ``bridge_alerts`` (the Jira bridge's alert records),
* any top-level provider key (``jira_*`` / ``*_jira_key``), and
* per-comment provider comment ids (``jira_comment_id`` / ``*_comment_id``).

Future providers add to the same shapes (a ``github_*`` key, a ``github_comment_id``),
so the conventions below cover them without a reconciler dependency.
"""

from __future__ import annotations

import copy
from typing import Any


def _is_provider_key(key: str) -> bool:
    """A top-level provider-id key (current: jira; future: github, …)."""
    return key.startswith("jira_") or key.endswith("_jira_key") or key == "jira_key"


def _is_provider_comment_id(key: str) -> bool:
    """A per-comment provider comment id (jira_comment_id and future *_comment_id)."""
    return key == "jira_comment_id" or key.endswith("_comment_id")


def strip_external(state: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ticket state with all external-tracker linkage removed.

    Non-mutating: the caller's state is untouched. Provenance (``source_*``) is
    OUR metadata, not external linkage, so it is preserved.
    """
    out = copy.deepcopy(state)
    out.pop("bridge_alerts", None)
    for key in list(out.keys()):
        if _is_provider_key(key):
            out.pop(key, None)
    comments = out.get("comments")
    if isinstance(comments, list):
        for entry in comments:
            if isinstance(entry, dict):
                for ckey in list(entry.keys()):
                    if _is_provider_comment_id(ckey):
                        entry.pop(ckey, None)
    return out
