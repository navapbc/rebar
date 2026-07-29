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
    """Cloud's rich-text codec: Atlassian Document Format, REST v3."""

    def fit_outbound(self, text: str) -> str:
        """Fit ``text`` so its ADF serialization stays within Jira's limit."""
        return adf.fit_text_to_adf_limit(text)

    def normalize_outbound(self, text: str) -> str:
        """Render ``text`` as it will read after an ADF round trip."""
        return adf.normalize_description(text)

    def to_wire(self, text: str) -> Any:
        """Convert ``text`` to an ADF document (Cloud REST v3's wire shape)."""
        return adf.text_to_adf(text)

    def decode_inbound(self, body: Any) -> str:
        """Decode an ADF document back to plain text; ``""`` for ``None``."""
        return adf.adf_to_text(body)
