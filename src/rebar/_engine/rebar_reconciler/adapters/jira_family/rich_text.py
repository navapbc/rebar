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


def cutover_clients() -> frozenset[str]:
    """Which clients send the RICH rich-text wire (story 3388, epic 708d).

    Resolved from ``reconciler.rich_text_cutover``: ``off`` (default) → nothing,
    ``cloud``/``dc`` → that one client, ``both`` → both. Read at CALL time, never at
    import time, so flipping the flag needs no redeploy and a module-level codec cannot
    freeze the answer.

    Fails CLOSED to the plain wire: an unreadable or absent config yields the empty set,
    so a config problem can never silently cut a client over. Config is imported lazily
    (the reconciler engine is stdlib-only and ships as subprocess package data, so it
    must stay importable without ``rebar`` on the path).
    """
    try:
        from rebar.config import ConfigError, resolve_rich_text_cutover
    except ImportError:
        return frozenset()
    try:
        return resolve_rich_text_cutover()
    except ConfigError:
        return frozenset()


class WikiTextCodec:
    """Data Center's rich-text codec: plain text / wiki markup, REST v2.

    Unlike Cloud's ADF, DC has no document-structure inflation to measure, so
    ``fit_outbound`` is a plain character truncation at ``WIKI_DESCRIPTION_LIMIT``.

    Two modes, selected by ``rich`` and NOT by editing this class:

    * ``rich=False`` (the DEFAULT, and what every existing caller gets) — fully
      identity, byte-for-byte today's wire.
    * ``rich=True`` — ``to_wire`` renders Markdown to wiki markup via story ``271c``'s
      segmenting renderer, and ``normalize_outbound`` is derived from it.

    The mode is driven by ``reconciler.rich_text_cutover`` (story 3388), which ships
    ``off``; setting it back to ``off`` restores the identity wire with no capability
    revert. Defaulting to ``False`` means a caller not routed through the flag cannot
    accidentally cut over. The DC codec is ONE-WAY and lossy, so loop prevention must
    never rest on ``decode(encode(x)) == x``; that is the echo-suppression layer's job.
    """

    def __init__(self, *, rich: bool = False) -> None:
        self._rich = rich

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
        """Render ``text`` as it will read after a Jira DC round trip.

        Identity in plain mode (wiki markup has no soft-wrap rejoin to undo). In rich
        mode this is ``decode_inbound(to_wire(text))`` BY CONSTRUCTION — and since DC's
        ``decode_inbound`` is the identity, that is exactly the rendered wiki. Deriving
        it from ``to_wire`` rather than writing a second normalizer is what keeps the
        codec law (ticket a32a) true for the lossy one-way DC codec.
        """
        if not self._rich or not isinstance(text, str):
            return text
        return self.decode_inbound(self.to_wire(text))

    def to_wire(self, text: str) -> Any:
        """The wire shape DC stores.

        Identity in plain mode. In rich mode the Markdown is rendered to wiki markup by
        the segmenting renderer from story ``271c``, which converts only what converts
        losslessly and passes everything else through byte-for-byte.
        """
        if not self._rich or not isinstance(text, str):
            return text
        from rebar_reconciler.adapters.jira_family.wiki_render import render_markdown_to_wiki

        return render_markdown_to_wiki(text)

    def decode_inbound(self, body: Any) -> str:
        """Identity on ``str``; ``""`` for ``None``."""
        if body is None:
            return ""
        return body
