"""Jira-family field sanitizers (story J2, epic e369).

Relocated from ``adapters/jira/jira_fields.py``. ``sanitize_label`` and
``sanitize_summary`` are pure and move outright — neither touches ADF or comment
limits. ``sanitize_description`` and ``sanitize_comment`` move in
CONTRACT-PARAMETERIZED form: their vendor dependency (the rich-text fit function /
the comment-truncation function + limit) is INJECTED rather than imported, so this
module never imports the Cloud-pinned ``adf.py`` / ``comment_limits.py``. Cloud
builds its own bound one-arg wrappers in ``adapters/jira/jira_fields.py`` from those
pinned modules. The Data Center backend builds its wrappers from its own
wiki-markup and plain-text equivalents.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from rebar_reconciler.adapters.jira_family.value_maps import (
    JIRA_LABEL_MAX_CHARS,
    JIRA_SUMMARY_MAX_CHARS,
)

logger = logging.getLogger(__name__)


class InvalidLabelError(ValueError):
    """A label value would be rejected by Jira (whitespace, comma, empty, oversize)."""


def sanitize_label(label: str) -> str:
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
        raise InvalidLabelError(f"Label must be str, got {type(label).__name__}: {label!r}")
    stripped = label.strip()
    if not stripped:
        raise InvalidLabelError(f"Label is empty after strip: {label!r}")
    if any(c.isspace() for c in stripped):
        raise InvalidLabelError(
            f"Label contains internal whitespace (not allowed by Jira): {label!r}"
        )
    if "," in stripped:
        raise InvalidLabelError(f"Label contains comma (not allowed by Jira): {label!r}")
    if len(stripped) > JIRA_LABEL_MAX_CHARS:
        raise InvalidLabelError(
            f"Label exceeds Jira's {JIRA_LABEL_MAX_CHARS}-char limit "
            f"({len(stripped)} chars): {label!r}"
        )
    return stripped


def sanitize_summary(summary: str) -> str:
    """Validate and truncate a Jira summary string.

    Jira's REST API rejects summaries > 255 chars with a confusing error.
    We truncate with a visible '... [truncated]' suffix so the reconciler
    can complete the mutation rather than crashing the pass on a single
    oversize ticket. Truncation is reversible (an operator can update the
    ticket later); reconciler crashes are not.

    A truncation warning is emitted so the operator can investigate.
    """
    if not isinstance(summary, str):
        raise ValueError(f"Summary must be str, got {type(summary).__name__}: {summary!r}")
    stripped = summary.strip()
    if not stripped:
        raise ValueError(f"Summary is empty after strip: {summary!r}")
    if len(stripped) <= JIRA_SUMMARY_MAX_CHARS:
        return stripped
    suffix = " [truncated]"
    keep = JIRA_SUMMARY_MAX_CHARS - len(suffix)
    truncated = stripped[:keep] + suffix
    logger.warning(
        "Summary exceeded Jira's %d-char limit (%d chars); truncated to %d chars",
        JIRA_SUMMARY_MAX_CHARS,
        len(stripped),
        len(truncated),
    )
    return truncated


def sanitize_description(description: str, *, fit: Callable[[str], str]) -> str:
    """Truncate an over-length description so its ADF representation fits Jira.

    Jira enforces the description limit on the ADF document, not the plain text, and
    ACLI surfaces an over-length ADF as a create/edit failure that aborts the WHOLE
    reconciler pass (bug 626d follow-up — a 46k-char epic, whose ADF was ~50k, killed
    a live cutover pass). ``fit`` is the vendor's rich-text-fit contract — INJECTED
    rather than imported, so this shared layer never imports the Cloud-pinned
    ``adf.py`` (J3's ``RichTextCodec`` seam formalizes this contract; here it is the
    minimal ``Callable[[str], str]`` form). The caller must apply the SAME transform
    on the differ's description-comparison path so the diff converges. Send-side
    only — the local store is never mutated; a warning is emitted so an operator can
    investigate.
    """
    if not isinstance(description, str):
        raise ValueError(
            f"Description must be str, got {type(description).__name__}: {description!r}"
        )
    fitted = fit(description)
    if len(fitted) != len(description):
        logger.warning(
            "Description exceeded the vendor's rich-text limit (%d plain chars); "
            "truncated to %d chars so its rich-text representation fits",
            len(description),
            len(fitted),
        )
    return fitted


def sanitize_comment(body: str, *, truncate: Callable[[str], str], max_chars: int) -> str:
    """Truncate an over-length comment body to fit Jira's hard limit.

    Bug 6afc-20ee-84e5-4dd5. Jira Cloud rejects comment bodies > 32,767 chars,
    but ``acli ... comment create`` exits 0 on the rejection; ``_check_mutation_
    failure`` then raises ``AcliMutationError`` and the comment never lands —
    driving the outbound comment-sync loop (re-emitted every pass). Truncating
    here (mirroring ``sanitize_summary``) lets the comment land.

    ``truncate`` and ``max_chars`` are the vendor's comment-limit contract —
    INJECTED rather than imported, so this shared layer never imports the
    Cloud-pinned ``comment_limits.py``. The caller's differ comparison path must
    apply the IDENTICAL ``truncate`` so the diff converges. A truncation warning
    is emitted so an operator can investigate; the local ticket store is never
    mutated (truncation is in-memory, send-side only).
    """
    if not isinstance(body, str):
        raise ValueError(f"Comment body must be str, got {type(body).__name__}: {body!r}")
    truncated = truncate(body)
    if truncated is not body and len(truncated) != len(body):
        logger.warning(
            "Comment exceeded the vendor's %d-char limit (%d chars); truncated to %d chars",
            max_chars,
            len(body),
            len(truncated),
        )
    return truncated
