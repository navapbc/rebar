"""Plain-text rich-text handling for Jira Data Center (no ADF).

Jira Server / Data Center's REST v2 carries issue descriptions and comment bodies as
plain text (wiki markup), NOT the Atlassian Document Format (ADF) dict that Cloud's v3
requires. So the outbound fit is a character-count truncation and the inbound decode is
an identity passthrough, in contrast to ``adapters/jira/adf.py``.
"""

from __future__ import annotations

_JIRA_DC_TEXT_LIMIT = 32767
_TRUNCATION_SUFFIX = " [truncated by reconciler]"


def fit_text_to_limit(text: str, *, limit: int = _JIRA_DC_TEXT_LIMIT) -> str:
    """Truncate ``text`` to ``limit`` characters, appending a marker when cut."""
    if len(text) <= limit:
        return text
    keep = max(limit - len(_TRUNCATION_SUFFIX), 0)
    return text[:keep] + _TRUNCATION_SUFFIX


def plain_text_of(body: object) -> str:
    """Inbound decode: a Data Center body is already plain text / wiki markup."""
    return str(body) if body is not None else ""
