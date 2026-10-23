"""Share Jira comment truncation between send and diff paths.

Jira Cloud limits comment bodies to 32,767 characters. Both paths must compare
the same fitted body so a landed truncated comment is not re-emitted. Fitting
changes only in-memory send and comparison values, never the local ticket.
This helper is stdlib-only for package and by-path imports.
"""

from __future__ import annotations

# Jira Cloud comment body hard limit (verified against Jira Cloud REST API
# documentation, 2026: the v2/v3 comment `body` field rejects content beyond
# 32,767 chars). This is the INCLUSIVE max — a 32,767-char body is accepted, a
# 32,768-char body is rejected.
_JIRA_COMMENT_MAX_CHARS: int = 32767

# Visible marker appended to a truncated body so a Jira reader (and the differ's
# comparison key) can tell the body was shortened by the reconciler. The marker
# is counted against the limit so the final body never exceeds the cap.
_TRUNCATION_SUFFIX: str = " … [truncated by reconciler]"


def truncate_comment_body(body: str) -> str:
    """Truncate an over-length comment body to fit Jira's hard limit.

    Idempotent and deterministic: a body already within the limit is returned
    unchanged, and applying this function twice yields the same result as
    applying it once (the suffix-bearing truncated body is itself within the
    limit). Both properties are load-bearing for convergence — the differ
    applies this to the expected local body and must produce exactly the body
    that landed in Jira on the prior pass.

    Args:
        body: the comment body (already normalised to plain text by the caller).

    Returns:
        ``body`` unchanged when ``len(body) <= _JIRA_COMMENT_MAX_CHARS``;
        otherwise a truncated string of length ``_JIRA_COMMENT_MAX_CHARS`` whose
        final characters are ``_TRUNCATION_SUFFIX``.
    """
    if len(body) <= _JIRA_COMMENT_MAX_CHARS:
        return body
    keep = _JIRA_COMMENT_MAX_CHARS - len(_TRUNCATION_SUFFIX)
    return body[:keep] + _TRUNCATION_SUFFIX
