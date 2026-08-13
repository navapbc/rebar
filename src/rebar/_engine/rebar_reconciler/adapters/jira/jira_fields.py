#!/usr/bin/env python3
"""Cloud-side Jira contract construction (story J2, epic e369).

The Jira-family-general sanitizers and value maps that used to live here have
relocated to ``adapters/jira_family/`` under public names (story J2), so a second
Jira-family backend (Data Center) consumes one implementation instead of forking
Cloud's. This module SURVIVES as the thin Cloud-side site that constructs the two
contract-parameterized sanitizers by binding Cloud's vendor dependencies:

* ``_sanitize_description`` binds ``jira_family.sanitize_description`` to Cloud's
  ``AdfCodec.fit_outbound`` (the rich-text seam story J3 formalized into the full
  ``RichTextCodec``; here it is used in its minimal ``Callable[[str], str]`` form).
* ``_sanitize_comment`` binds ``jira_family.sanitize_comment`` to Cloud's
  ``comment_limits.truncate_comment_body`` / ``_JIRA_COMMENT_MAX_CHARS``.

Both stay one-arg functions so the existing ACLI call sites (``acli.py``,
``acli_cli_ops.py``) keep working unchanged. No other symbol is re-exported here —
callers of the pure sanitizers / value maps / link vocabulary import
``adapters.jira_family`` directly.
"""

from __future__ import annotations

from rebar_reconciler.adapters.jira.comment_limits import (  # shared send/diff truncation
    _JIRA_COMMENT_MAX_CHARS,
)
from rebar_reconciler.adapters.jira.comment_limits import (
    truncate_comment_body as _truncate_comment_body,
)
from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec
from rebar_reconciler.adapters.jira_family import sanitize_comment as _shared_sanitize_comment
from rebar_reconciler.adapters.jira_family import (
    sanitize_description as _shared_sanitize_description,
)
from rebar_reconciler.adapters.jira_family.rich_text import cutover_clients


def _sanitize_description(description: str) -> str:
    """Truncate an over-length description so its ADF representation fits Jira.

    Jira enforces the description limit on the ADF document, not the plain text, and
    ACLI surfaces an over-length ADF as a create/edit failure that aborts the WHOLE
    reconciler pass (bug 626d follow-up — a 46k-char epic, whose ADF was ~50k, killed
    a live cutover pass). Fits via ``AdfCodec.fit_outbound`` (story J3's
    ``RichTextCodec`` contract — the same underlying ADF-limit logic, now reached
    through the codec rather than imported directly), injected into
    ``jira_family.sanitize_description`` as Cloud's rich-text contract — FIT ONLY,
    no normalization, so this stays the distinct primitive the send path's
    ``_fit_description`` composes with normalization on top of. The differ's
    description comparison applies the IDENTICAL transform and the diff converges.
    Send-side only — the local store is never mutated; a warning is emitted so an
    operator can investigate.
    """
    return _shared_sanitize_description(
        description, fit=AdfCodec(rich="cloud" in cutover_clients()).fit_outbound
    )


def _sanitize_comment(body: str) -> str:
    """Truncate an over-length comment body to fit Jira's hard limit.

    Bug 6afc-20ee-84e5-4dd5. Jira Cloud rejects comment bodies > 32,767 chars,
    but ``acli ... comment create`` exits 0 on the rejection; ``_check_mutation_
    failure`` then raises ``AcliMutationError`` and the comment never lands —
    driving the outbound comment-sync loop (re-emitted every pass). Truncating
    here lets the comment land.

    The actual truncation rule lives in the shared ``rebar_reconciler.adapters.jira.
    comment_limits.truncate_comment_body`` helper, injected into ``jira_family.
    sanitize_comment`` as Cloud's comment-limit contract, so the differ's comparison
    path (``outbound_differ._diff_comments``) applies the IDENTICAL transform and the
    diff converges. A truncation warning is emitted so an operator can investigate;
    the local ticket store is never mutated (truncation is in-memory, send-side
    only).
    """
    return _shared_sanitize_comment(
        body, truncate=_truncate_comment_body, max_chars=_JIRA_COMMENT_MAX_CHARS
    )
