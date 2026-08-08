"""The ``RichTextCodec`` contract (story J3, epic e369).

Rich-text encoding is one of the three real differences between Jira Cloud and
Jira Data Center: Cloud's REST v3 requires Atlassian Document Format (a nested
JSON document; sending a plain string returns 400), while Data Center's REST v2
carries descriptions and comment bodies as plain text / wiki markup.

``RichTextCodec`` has FOUR operations, not two, because Cloud's existing
behaviour needs all four kept distinct:

* ``fit_outbound(text)``       — fit to the deployment's limit.
* ``normalize_outbound(text)`` — render the value as it will read after a round
                                 trip (Cloud rejoins soft wraps; DC is identity).
* ``to_wire(text)``            — the wire shape (Cloud: an ADF dict; DC: the str).
* ``decode_inbound(body)``     — wire shape back to plain text.

``fit_outbound`` and ``normalize_outbound`` are separate on purpose: Cloud's send
path (``backend.py``'s ``_fit_description``) composes BOTH, while the description
sanitizer (``jira_fields._sanitize_description``) applies only the fit.
Collapsing the two into one operation would silently change the observable
behaviour of whichever caller lost its distinct step.

This module holds the Protocol and the Data Center implementation
(``WikiTextCodec``) only. The Cloud implementation (``AdfCodec``) lives on the
Cloud side, in ``adapters/jira/rich_text_codec.py`` — this package imports
NOTHING from ``adapters/jira/`` (see ``adapters/jira_family/__init__.py``'s
module docstring and ``docs/adr/0083-reconciler-vendor-adapter-seam.md``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RichTextCodec(Protocol):
    """A deployment's rich-text contract: fit, normalize, encode, decode."""

    def fit_outbound(self, text: str) -> str:
        """Fit ``text`` to this deployment's rich-text length limit."""
        ...

    def normalize_outbound(self, text: str) -> str:
        """Render ``text`` as it will read after a round trip through Jira."""
        ...

    def to_wire(self, text: str) -> Any:
        """Return the wire shape ``text`` must be sent as."""
        ...

    def decode_inbound(self, body: Any) -> str:
        """Decode a wire-shaped rich-text body back to plain text."""
        ...


# Jira Server/Data Center's text-field cap. Same documented 32,767-character
# limit ``adapters.jira.comment_limits._JIRA_COMMENT_MAX_CHARS`` already uses
# for Cloud comment bodies in this codebase; DC's REST v2 description/comment
# fields carry plain text with the same hard cap.
WIKI_DESCRIPTION_LIMIT: int = 32767

# Visible marker appended to a truncated value so a Jira reader can tell it was
# shortened by the reconciler. Reuses the exact wording of
# ``adapters.jira.adf._ADF_TRUNCATION_SUFFIX`` (defined independently here, NOT
# imported — this package must never import from ``adapters/jira/``), so a
# reader sees one consistent marker regardless of deployment. The marker is
# counted against the limit so the fitted value never exceeds the cap
# (mirroring ``comment_limits.truncate_comment_body``'s rule).
_WIKI_TRUNCATION_SUFFIX: str = " … [truncated by reconciler]"


class WikiTextCodec:
    """Data Center's rich-text codec: plain text / wiki markup, REST v2.

    Unlike Cloud's ADF, DC has no document-structure inflation to measure, so
    ``fit_outbound`` is a plain character truncation at ``WIKI_DESCRIPTION_LIMIT``,
    and ``normalize_outbound`` is the identity (wiki markup has no soft-wrap
    rejoin transform to undo). ``to_wire``/``decode_inbound`` are the identity
    on ``str`` since DC's wire shape already IS plain text.
    """

    def fit_outbound(self, text: str) -> str:
        """Truncate ``text`` to ``WIKI_DESCRIPTION_LIMIT`` characters.

        Idempotent and deterministic: a value already within the limit is
        returned unchanged, and applying this twice yields the same result as
        applying it once (mirrors ``comment_limits.truncate_comment_body``).
        Non-``str`` values pass through untouched, matching the mapper's
        existing "never coerce" behaviour.
        """
        if not isinstance(text, str) or len(text) <= WIKI_DESCRIPTION_LIMIT:
            return text
        keep = WIKI_DESCRIPTION_LIMIT - len(_WIKI_TRUNCATION_SUFFIX)
        return text[:keep] + _WIKI_TRUNCATION_SUFFIX

    def normalize_outbound(self, text: str) -> str:
        """Identity: wiki markup has no soft-wrap rejoin to normalize."""
        return text

    def to_wire(self, text: str) -> Any:
        """Identity: DC's wire shape for rich text already is the plain ``str``."""
        return text

    def decode_inbound(self, body: Any) -> str:
        """Identity on ``str``; ``""`` for ``None``."""
        if body is None:
            return ""
        return body
