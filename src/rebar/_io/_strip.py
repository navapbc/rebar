"""Strip external-provider linkage while retaining source provenance.

Remove top-level bridge alerts and provider ID keys plus per-comment provider IDs, so an
export re-imports without foreign associations. Naming patterns cover future providers
without coupling this seam to a reconciler.
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
