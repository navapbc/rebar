"""Shared Jira comment-length truncation helper (bug 6afc-20ee-84e5-4dd5).

The Jira Cloud comment body has a documented hard limit of 32,767 characters.
``acli jira workitem comment create`` exits 0 even when the underlying Jira
operation fails on an over-length body, so ``_check_mutation_failure`` raises
``AcliMutationError`` — the comment never lands. Because the outbound differ
decides a comment needs adding when the local body is *not present* in Jira,
the never-landed comment is re-emitted on every reconciler pass (the outbound
comment-sync loop).

The fix has a convergence requirement that forces a SINGLE shared truncation
function used by BOTH paths:

  - the send path (``acli-integration.add_comment``) truncates the body before
    handing it to ACLI, so the comment actually lands; and
  - the differ comparison path (``outbound_differ._diff_comments``) applies the
    SAME truncation to the expected local body BEFORE the membership test, so a
    previously-truncated-then-landed Jira body matches and the diff stops
    re-emitting.

If the two paths used different truncation logic they could never agree on the
landed body, and the loop would persist. They MUST therefore share this one
helper so they cannot drift.

CONSTRAINT (hard): truncation applies ONLY to the in-memory body sent to Jira
and the differ's in-memory comparison key. The truncated body is NEVER written
back to the local ticket store — local comment content is the source of truth
and stays untouched.

This module is stdlib-only and pure so both the package-imported caller
(``acli-integration.py``) and the importlib-by-path caller
(``outbound_differ.py``, in tests) can load it without side effects.
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
