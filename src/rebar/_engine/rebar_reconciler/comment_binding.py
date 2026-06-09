"""Comment identity binding for bidirectional Jira <-> local sync.

Matches local ticket comments to Jira comments by binding ID.
See ``docs/contracts/comment-binding-schema.md`` (relative to plugin root) for the
full binding lifecycle and conflict-resolution rules.
"""

from __future__ import annotations


def match_comments(local_comments: list[dict], jira_comments: list[dict]) -> dict:
    """Match local comments to Jira comments by binding ID.

    Local comments carry an optional ``jira_comment_id`` field.
    Jira comments carry an ``id`` field.

    Returns::

        {
            "bound": [(local_idx, jira_idx), ...],  # matched pairs
            "local_only": [local_idx, ...],          # outbound create candidates
            "jira_only": [jira_idx, ...],            # inbound create candidates
        }
    """
    # Build a lookup from Jira comment id -> index
    jira_id_to_idx: dict[str, int] = {}
    for idx, jc in enumerate(jira_comments):
        jid = jc.get("id")
        if jid is not None:
            jira_id_to_idx[str(jid)] = idx

    bound: list[tuple[int, int]] = []
    local_only: list[int] = []
    matched_jira_indices: set[int] = set()

    for local_idx, lc in enumerate(local_comments):
        binding = lc.get("jira_comment_id")
        if binding is not None:
            binding_str = str(binding)
            jira_idx = jira_id_to_idx.get(binding_str)
            if jira_idx is not None:
                bound.append((local_idx, jira_idx))
                matched_jira_indices.add(jira_idx)
            else:
                # Binding points to a Jira comment that no longer exists
                # (tombstone case). Treat as local-only for the caller
                # to decide whether to re-create or discard.
                local_only.append(local_idx)
        else:
            local_only.append(local_idx)

    jira_only: list[int] = [
        idx for idx in range(len(jira_comments)) if idx not in matched_jira_indices
    ]

    return {
        "bound": bound,
        "local_only": local_only,
        "jira_only": jira_only,
    }
