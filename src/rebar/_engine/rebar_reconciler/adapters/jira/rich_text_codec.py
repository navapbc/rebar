"""``AdfCodec`` — Cloud's ``RichTextCodec`` implementation (story J3, epic e369).

A pass-through wrapper over the location-pinned ``adapters/jira/adf.py``. It
reimplements nothing: each operation delegates directly to the existing ADF
function, so ``AdfCodec`` is proven to be a wrapper rather than a second copy
of the encoding logic (see ``test_rich_text_codec.py``'s
``test_adf_codec_is_a_pass_through_to_the_pinned_adf_module``).

``AdfCodec`` lives here, on the Cloud side, and NOT in ``adapters/jira_family/``:
that package's import contract forbids importing any Cloud vendor module
(including ``adf.py``), so a codec that wraps ``adf.py`` must be constructed
where ``adf.py`` may be imported.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler.adapters.jira import adf


class AdfCodec:
    """Cloud's rich-text codec: Atlassian Document Format, REST v3.

    Two modes, selected by ``rich`` and NOT by editing this class:

    * ``rich=False`` (the DEFAULT, and what every existing caller gets) — the
      historical plain-text pass-through over ``adf.py``. Byte-for-byte today's wire.
    * ``rich=True`` — the Markdown-aware wire from story ``e59d``: Cloud renders real
      headings/lists/code instead of literal Markdown source.

    The mode is driven by ``reconciler.rich_text_cutover`` (story 3388), which ships
    ``off``; setting it back to ``off`` restores the plain wire with no capability
    revert. Defaulting the parameter to ``False`` means a caller that has not been
    routed through the flag cannot accidentally cut over.

    In BOTH modes ``normalize_outbound(t) == decode_inbound(to_wire(t))`` holds by
    CONSTRUCTION in rich mode, rather than by a parallel normalizer that could drift
    from the wire (the codec law, ticket a32a, pinned by
    ``test_rich_text_codec.py:163``).
    """

    def __init__(self, *, rich: bool = False) -> None:
        self._rich = rich

    def fit_outbound(self, text: str) -> str:
        """Fit ``text`` so its serialized wire stays within Jira's limit.

        Rich mode measures the MARKDOWN-AWARE serialization: that document inflates
        differently, so fitting it with the plain measure could still exceed the cap.
        """
        if self._rich:
            return adf.fit_markdown_to_adf_limit(text)
        return adf.fit_text_to_adf_limit(text)

    def normalize_outbound(self, text: str) -> str:
        """Render ``text`` as it will read after an ADF round trip."""
        if self._rich:
            return adf.adf_to_markdown(adf.markdown_to_adf(text))
        return adf.normalize_description(text)

    def to_wire(self, text: str) -> Any:
        """Convert ``text`` to an ADF document (Cloud REST v3's wire shape)."""
        if self._rich:
            return adf.markdown_to_adf(text)
        return adf.text_to_adf(text)

    def decode_inbound(self, body: Any) -> str:
        """Decode an ADF document back to text; ``""`` for ``None``."""
        if self._rich:
            return adf.adf_to_markdown(body)
        return adf.adf_to_text(body)
