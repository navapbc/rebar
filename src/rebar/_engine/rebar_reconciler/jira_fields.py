#!/usr/bin/env python3
"""Jira field sanitization + local↔Jira value maps.

Pure, dependency-light field helpers shared by the ACLI client core
(``acli.py``), the module-level CLI ops (``acli_cli_ops.py``), and the graph
mixin (``acli_graph.py``): label/summary/comment sanitizers that defend against
Jira's hard limits and malformed input, plus the local→Jira priority and status
value maps.

No external dependencies beyond the shared ``comment_limits`` helper — stdlib
only.
"""

from __future__ import annotations

import logging

from rebar_reconciler.comment_limits import (  # shared send/diff truncation
    _JIRA_COMMENT_MAX_CHARS,
    truncate_comment_body as _truncate_comment_body,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Value maps
# ---------------------------------------------------------------------------

# Local priority integer (0-4) → Jira priority name.
_LOCAL_PRIORITY_TO_JIRA: dict[int, str] = {
    0: "Highest",
    1: "High",
    2: "Medium",
    3: "Low",
    4: "Lowest",
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
_JIRA_SUMMARY_MAX_CHARS: int = 254
_JIRA_LABEL_MAX_CHARS: int = 255

# Local status string → Jira workflow state name.
# status.capitalize() produces "In_progress" for snake_case inputs; this mapping
# ensures correct Jira state names are used in ACLI transition commands.
# ticket 929a: blocked/cancelled map to the nearest live DIG workflow state
# ({To Do, In Progress, In Review, Done} only); lossless information is
# preserved via rebar-status: annotation labels managed by outbound_differ.
_LOCAL_STATUS_TO_JIRA: dict[str, str] = {
    "open": "To Do",
    "in_progress": "In Progress",
    "closed": "Done",
    "blocked": "In Progress",
    "cancelled": "Done",
}


# ---------------------------------------------------------------------------
# Sanitizers
# ---------------------------------------------------------------------------


class InvalidLabelError(ValueError):
    """A label value would be rejected by Jira (whitespace, comma, empty, oversize)."""


def _sanitize_label(label: str) -> str:
    """Validate a Jira label, raising InvalidLabelError on rejection.

    Jira labels are single tokens — no whitespace, no commas, non-empty, length
    <= 255 chars. ACLI does not validate client-side; sending an invalid label
    surfaces as a confusing server-side error or (worse) silently corrupts the
    label set. We sanitize here so the reconciler fails fast with a clear
    message instead of issuing a malformed mutation against live Jira.

    Whitespace is stripped from the input before validation. A label that
    contains internal whitespace (e.g., "with space") is REJECTED rather than
    silently mangled — the reconciler should never invent a label name that
    differs from what the caller asked for.
    """
    if not isinstance(label, str):
        raise InvalidLabelError(
            f"Label must be str, got {type(label).__name__}: {label!r}"
        )
    stripped = label.strip()
    if not stripped:
        raise InvalidLabelError(f"Label is empty after strip: {label!r}")
    if any(c.isspace() for c in stripped):
        raise InvalidLabelError(
            f"Label contains internal whitespace (not allowed by Jira): {label!r}"
        )
    if "," in stripped:
        raise InvalidLabelError(
            f"Label contains comma (not allowed by Jira): {label!r}"
        )
    if len(stripped) > _JIRA_LABEL_MAX_CHARS:
        raise InvalidLabelError(
            f"Label exceeds Jira's {_JIRA_LABEL_MAX_CHARS}-char limit "
            f"({len(stripped)} chars): {label!r}"
        )
    return stripped


def _sanitize_summary(summary: str) -> str:
    """Validate and truncate a Jira summary string.

    Jira's REST API rejects summaries > 255 chars with a confusing error.
    We truncate with a visible '... [truncated]' suffix so the reconciler
    can complete the mutation rather than crashing the pass on a single
    oversize ticket. Truncation is reversible (an operator can update the
    ticket later); reconciler crashes are not.

    A truncation warning is emitted so the operator can investigate.
    """
    if not isinstance(summary, str):
        raise ValueError(
            f"Summary must be str, got {type(summary).__name__}: {summary!r}"
        )
    stripped = summary.strip()
    if not stripped:
        raise ValueError(f"Summary is empty after strip: {summary!r}")
    if len(stripped) <= _JIRA_SUMMARY_MAX_CHARS:
        return stripped
    suffix = " [truncated]"
    keep = _JIRA_SUMMARY_MAX_CHARS - len(suffix)
    truncated = stripped[:keep] + suffix
    logger.warning(
        "Summary exceeded Jira's %d-char limit (%d chars); truncated to %d chars",
        _JIRA_SUMMARY_MAX_CHARS,
        len(stripped),
        len(truncated),
    )
    return truncated


def _sanitize_comment(body: str) -> str:
    """Truncate an over-length comment body to fit Jira's hard limit.

    Bug 6afc-20ee-84e5-4dd5. Jira Cloud rejects comment bodies > 32,767 chars,
    but ``acli ... comment create`` exits 0 on the rejection; ``_check_mutation_
    failure`` then raises ``AcliMutationError`` and the comment never lands —
    driving the outbound comment-sync loop (re-emitted every pass). Truncating
    here (mirroring ``_sanitize_summary``) lets the comment land.

    The actual truncation rule lives in the shared ``rebar_reconciler.comment_
    limits.truncate_comment_body`` helper so the differ's comparison path
    (``outbound_differ._diff_comments``) applies the IDENTICAL transform and the
    diff converges. A truncation warning is emitted so an operator can
    investigate; the local ticket store is never mutated (truncation is
    in-memory, send-side only).
    """
    if not isinstance(body, str):
        raise ValueError(
            f"Comment body must be str, got {type(body).__name__}: {body!r}"
        )
    truncated = _truncate_comment_body(body)
    if truncated is not body and len(truncated) != len(body):
        logger.warning(
            "Comment exceeded Jira's %d-char limit (%d chars); truncated to %d chars",
            _JIRA_COMMENT_MAX_CHARS,
            len(body),
            len(truncated),
        )
    return truncated
