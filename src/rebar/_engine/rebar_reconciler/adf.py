"""ADF (Atlassian Document Format) round-trip conversion.

Standalone module with no external dependencies (stdlib only).

- ``adf_to_text``: walk ADF nodes and produce readable plain text.
- ``text_to_adf``: convert plain text to minimal ADF doc.

Round-trip property:
  ``adf_to_text(text_to_adf(text)) == text``  (lossless for plain text)
  ``text_to_adf(adf_to_text(doc))`` will NOT reproduce the original ADF
  (lossy by design -- tables become pipe-delimited, formatting becomes markdown).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# ADF -> text
# ---------------------------------------------------------------------------


def adf_to_text(adf_doc: dict | None) -> str:
    """Walk ADF nodes and produce readable plain text.

    Handles all Jira Cloud v3 node types.  Unknown nodes emit
    ``[unsupported: <type>]`` so no data is silently dropped.
    """
    if not adf_doc:
        return ""
    return _walk_node(adf_doc).rstrip("\n")


def _walk_node(node: dict) -> str:
    """Recursively convert a single ADF node to text."""
    ntype = node.get("type", "")
    handler = _NODE_HANDLERS.get(ntype, _handle_unknown)
    return handler(node)


def _walk_children(node: dict) -> str:
    """Walk all children of a node and concatenate their text."""
    content = node.get("content") or []
    return "".join(_walk_node(child) for child in content)


# -- Individual node handlers ------------------------------------------------


def _handle_doc(node: dict) -> str:
    return _walk_children(node)


def _handle_paragraph(node: dict) -> str:
    return _walk_children(node) + "\n"


def _handle_text(node: dict) -> str:
    text = node.get("text", "")
    marks = node.get("marks") or []
    for mark in marks:
        mtype = mark.get("type", "")
        if mtype == "strong":
            text = f"**{text}**"
        elif mtype == "em":
            text = f"*{text}*"
        elif mtype == "code":
            text = f"`{text}`"
    return text


def _handle_heading(node: dict) -> str:
    level = (node.get("attrs") or {}).get("level", 1)
    content = _walk_children(node)
    return "#" * level + " " + content + "\n"


def _handle_bullet_list(node: dict) -> str:
    items = node.get("content") or []
    lines: list[str] = []
    for item in items:
        item_text = _walk_children(item).rstrip("\n")
        lines.append(f"- {item_text}")
    return "\n".join(lines) + "\n"


def _handle_ordered_list(node: dict) -> str:
    items = node.get("content") or []
    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        item_text = _walk_children(item).rstrip("\n")
        lines.append(f"{idx}. {item_text}")
    return "\n".join(lines) + "\n"


def _handle_list_item(node: dict) -> str:
    return _walk_children(node)


def _handle_code_block(node: dict) -> str:
    content = _walk_children(node).rstrip("\n")
    return f"```\n{content}\n```\n"


def _handle_blockquote(node: dict) -> str:
    content = _walk_children(node).rstrip("\n")
    lines = content.split("\n")
    return "\n".join(f"> {line}" for line in lines) + "\n"


def _handle_hard_break(_node: dict) -> str:
    return "\n"


def _handle_mention(node: dict) -> str:
    attrs = node.get("attrs") or {}
    text = attrs.get("text", "unknown")
    return f"@{text}"


def _handle_inline_card(node: dict) -> str:
    attrs = node.get("attrs") or {}
    url = attrs.get("url", "")
    return f"[link]({url})"


def _handle_rule(_node: dict) -> str:
    return "---\n"


def _handle_table(node: dict) -> str:
    rows = node.get("content") or []
    lines: list[str] = []
    for row in rows:
        cells = row.get("content") or []
        cell_texts = []
        for cell in cells:
            cell_text = _walk_children(cell).rstrip("\n")
            cell_texts.append(cell_text)
        lines.append("| " + " | ".join(cell_texts) + " |")
    return "\n".join(lines) + "\n"


def _handle_table_row(node: dict) -> str:
    # Handled by _handle_table; direct calls fall through here.
    cells = node.get("content") or []
    cell_texts = [_walk_children(c).rstrip("\n") for c in cells]
    return "| " + " | ".join(cell_texts) + " |\n"


def _handle_table_cell(node: dict) -> str:
    return _walk_children(node)


def _handle_media(node: dict) -> str:
    attrs = node.get("attrs") or {}
    media_id = attrs.get("id", "attachment")
    return f"[media: {media_id}]\n"


def _handle_panel(node: dict) -> str:
    content = _walk_children(node).rstrip("\n")
    lines = content.split("\n")
    return "\n".join(f"> [panel] {line}" for line in lines) + "\n"


def _handle_expand(node: dict) -> str:
    return _walk_children(node)


def _handle_emoji(node: dict) -> str:
    attrs = node.get("attrs") or {}
    short_name = attrs.get("shortName")
    if short_name:
        return short_name
    return attrs.get("text", attrs.get("fallback", ""))


def _handle_status(node: dict) -> str:
    attrs = node.get("attrs") or {}
    text = attrs.get("text", "")
    return f"[STATUS: {text}]"


def _handle_date(node: dict) -> str:
    attrs = node.get("attrs") or {}
    ts = attrs.get("timestamp", "")
    try:
        # Jira stores epoch millis as a string
        epoch_ms = int(ts)
        dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return str(ts)


def _handle_unknown(node: dict) -> str:
    ntype = node.get("type", "unknown")
    return f"[unsupported: {ntype}]"


# -- Handler dispatch table --------------------------------------------------

_NODE_HANDLERS: dict[str, Any] = {
    "doc": _handle_doc,
    "paragraph": _handle_paragraph,
    "text": _handle_text,
    "heading": _handle_heading,
    "bulletList": _handle_bullet_list,
    "orderedList": _handle_ordered_list,
    "listItem": _handle_list_item,
    "codeBlock": _handle_code_block,
    "blockquote": _handle_blockquote,
    "hardBreak": _handle_hard_break,
    "mention": _handle_mention,
    "inlineCard": _handle_inline_card,
    "rule": _handle_rule,
    "table": _handle_table,
    "tableRow": _handle_table_row,
    "tableCell": _handle_table_cell,
    "tableHeader": _handle_table_cell,  # same as tableCell
    "mediaSingle": _handle_media,
    "mediaGroup": _handle_media,
    "panel": _handle_panel,
    "expand": _handle_expand,
    "emoji": _handle_emoji,
    "status": _handle_status,
    "date": _handle_date,
}


# ---------------------------------------------------------------------------
# text -> ADF
# ---------------------------------------------------------------------------


def text_to_adf(text: str) -> dict[str, Any]:
    """Convert a plain text string to Atlassian Document Format (ADF).

    Jira REST API v3 (used by ACLI Go v1.3+) requires the ``description``
    field to be an ADF object, not a plain string.
    """
    paragraphs: list[dict[str, Any]] = []
    for line in text.split("\n"):
        if line:
            paragraphs.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
        else:
            paragraphs.append({"type": "paragraph", "content": []})
    return {"type": "doc", "version": 1, "content": paragraphs}
