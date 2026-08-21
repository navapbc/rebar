#!/usr/bin/env python3
"""Cloud-side Jira contract construction (story J2, epic e369).

The Jira-family-general sanitizers and value maps that used to live here have
relocated to ``adapters/jira_family/`` under public names (story J2), so a second
Jira-family backend (Data Center) consumes one implementation instead of forking
Cloud's. This module SURVIVES as the thin Cloud-side site that constructs the
contract-parameterized sanitizer by binding Cloud's vendor dependencies:

* ``_sanitize_description`` binds ``jira_family.sanitize_description`` to Cloud's
  ``AdfCodec.fit_outbound`` (the rich-text seam story J3 formalized into the full
  ``RichTextCodec``; here it is used in its minimal ``Callable[[str], str]`` form).

(A ``_sanitize_comment`` twin used to be constructed here too, but it never had a
production caller — Cloud's REAL send fit is ``fit_preserving_marker`` over
``AdfCodec.fit_outbound`` inside ``acli_cli_ops.add_comment``, which measures the
SERIALIZED ADF, while the twin applied a plain character cap that would have cut
the RECONCILER_MARKER. Bug b9b4-f460-2d54-4872 removed the false assurance.)

It stays a one-arg function so the existing ACLI call sites (``acli.py``,
``acli_cli_ops.py``) keep working unchanged. No other symbol is re-exported here —
callers of the pure sanitizers / value maps / link vocabulary import
``adapters.jira_family`` directly.
"""

from __future__ import annotations

from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec
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
