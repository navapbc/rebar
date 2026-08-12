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

import json
import re
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


# A line that opens a markdown block construct. Such a line is never joined onto
# its neighbour: it is separated by a ``hardBreak`` (which ``adf_to_text``
# decodes back to exactly "\n"), so the block structure survives the round trip.
_BLOCK_MARKER_RE = re.compile(r"^(?:[-*+](?=\s|$)|\d+[.)](?=\s|$)|#|>|\||```)")


def _is_structural(line: str, *, in_fence: bool) -> bool:
    """True when ``line`` must keep its own line rather than be soft-wrap joined."""
    if in_fence:
        return True
    if line != line.lstrip():
        return True  # indented continuation
    return bool(_BLOCK_MARKER_RE.match(line))


def _paragraph_from_block(block: list[tuple[str, bool]]) -> dict[str, Any]:
    """Build ONE ``paragraph`` node from a block of ``(line, is_structural)`` pairs.

    Consecutive soft-wrapped prose lines are stripped and joined with a single
    space; every other adjacency is separated by a ``hardBreak`` node.
    """
    content: list[dict[str, Any]] = []
    for idx, (line, structural) in enumerate(block):
        text = line if structural else line.strip()
        if idx:
            if structural or block[idx - 1][1]:
                content.append({"type": "hardBreak"})
            else:
                # Soft-wrapped continuation of the previous prose line.
                content[-1]["text"] += " " + text
                continue
        content.append({"type": "text", "text": text})
    return {"type": "paragraph", "content": content}


def text_to_adf(text: str) -> dict[str, Any]:
    """Convert a plain text string to Atlassian Document Format (ADF).

    Jira REST API v3 (used by ACLI Go v1.3+) requires the ``description``
    field to be an ADF object, not a plain string.

    Rebar descriptions are authored hard-wrapped at a fixed column width. Emitting
    one paragraph per SOURCE line (the historical behaviour) made Jira render a
    visible break at the end of every wrapped line. Instead the text is split into
    blocks on blank lines, and each non-empty block becomes exactly ONE paragraph
    whose soft-wrapped prose lines are rejoined with a single space. Blank lines
    still emit an empty paragraph, which is what carries blank-line separation back
    through ``adf_to_text``. The transform is idempotent — encoding an already
    decoded value reproduces it — so the description differ converges instead of
    re-emitting an update on every reconcile pass.
    """
    nodes: list[dict[str, Any]] = []
    block: list[tuple[str, bool]] = []
    in_fence = False
    for line in text.split("\n"):
        if not line.strip():
            if block:
                nodes.append(_paragraph_from_block(block))
                block = []
            nodes.append({"type": "paragraph", "content": []})
            continue
        structural = _is_structural(line, in_fence=in_fence)
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        block.append((line, structural))
    if block:
        nodes.append(_paragraph_from_block(block))
    return {"type": "doc", "version": 1, "content": nodes}


def normalize_description(text: str) -> str:
    """Return ``text`` as it will read after a Jira ADF round trip.

    ``text_to_adf`` rejoins soft-wrapped prose, so the body Jira stores — and
    therefore the value a later fetch decodes back — is the JOINED form while the
    local rebar description stays hard-wrapped. Applying this identical
    normalization to the local value before a description comparison keeps the
    differ convergent (same precedent as ``fit_text_to_adf_limit``). Idempotent:
    a value already normalized is returned unchanged. Send/diff-side only; the
    local ticket store is never mutated.
    """
    if not isinstance(text, str):
        return text
    return adf_to_text(text_to_adf(text))


# Jira enforces the description-field length limit on the ADF representation, NOT
# the plain text. ``text_to_adf`` wraps every line in its own paragraph node, so a
# multi-line description inflates well past its plain-text length (a 32,767-char
# description observed serializing to ~50,566 ADF chars — bug 626d follow-up).
# Target a budget safely under Jira's 32,767-char limit (margin for serialization
# differences between this measurement and ACLI's wire form).
_ADF_DESCRIPTION_LIMIT: int = 32000
_ADF_TRUNCATION_SUFFIX: str = " … [truncated by reconciler]"


def _adf_serialized_len(text: str) -> int:
    """Length of ``text``'s ADF serialization — the size Jira actually limits."""
    return len(json.dumps(text_to_adf(text)))


def fit_text_to_adf_limit(text: str, *, limit: int = _ADF_DESCRIPTION_LIMIT) -> str:
    """Truncate ``text`` so its ADF serialization fits within ``limit`` chars.

    A plain-text cap is insufficient because ADF structure inflates the size, so we
    binary-search the largest text prefix whose ADF (plus a truncation marker)
    serializes within ``limit``. Idempotent and deterministic: a value already
    within ``limit`` is returned unchanged, and the function is its own fixed point —
    so the send path (``acli`` create/update) and the differ's description comparison
    apply it identically and the diff converges. Send/diff-side only; the local
    ticket store is never mutated.
    """
    if not isinstance(text, str) or _adf_serialized_len(text) <= limit:
        return text
    lo, hi, best = 0, len(text), _ADF_TRUNCATION_SUFFIX
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid] + _ADF_TRUNCATION_SUFFIX
        if _adf_serialized_len(candidate) <= limit:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


# ---------------------------------------------------------------------------
# Markdown-aware ADF (story e59d, epic 708d)
#
# Everything above converts PLAIN text: ``text_to_adf`` emits one paragraph per
# block, so Markdown syntax (headings, list markers, fences, ``**bold**``) reaches
# Jira as literal characters and Cloud renders raw source. The functions below add
# the missing Markdown-aware half, backed by ``marklas`` (MIT) over ``mistune``
# (BSD-3-Clause).
#
# marklas is an OPTIONAL EXTRA, imported LAZILY inside each function. This module
# ships as reconciler package data under ``rebar/_engine/`` and is contractually
# stdlib-only, so ``import adf`` must keep working with no extras installed; when
# marklas is absent each function returns its PLAIN counterpart's result rather
# than raising. (The vendoring alternative was rejected: marklas needs mistune, and
# ``src/rebar/_vendor/`` is not importable from the reconciler subprocess.)
#
# NOTHING here is wired into ``AdfCodec`` or any live send path — that cutover is
# story 3388, which also owns tolerating a no-extras install.
# ---------------------------------------------------------------------------

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _marklas() -> Any | None:
    """Return the ``marklas`` module, or ``None`` when the extra is not installed."""
    try:
        import marklas
    except ImportError:
        return None
    return marklas


def _canonicalize_marks(node: Any) -> Any:
    """Sort every text node's ``marks`` by type, in place, and return ``node``.

    Co-located marks have no canonical order in ADF, so ``**~~x~~**`` and
    ``~~**x**~~`` encode to the same node set in a different sequence — and
    round-tripping alternates between the two spellings forever (a measured exact
    2-cycle that never converges). Sorting by mark type collapses the pair to one
    deterministic form, which is what makes repeated encode/decode a fixed point.
    """
    if isinstance(node, dict):
        marks = node.get("marks")
        if isinstance(marks, list):
            node["marks"] = sorted(
                marks, key=lambda m: m.get("type", "") if isinstance(m, dict) else ""
            )
        for child in node.get("content", []) or []:
            _canonicalize_marks(child)
    elif isinstance(node, list):
        for child in node:
            _canonicalize_marks(child)
    return node


def markdown_to_adf(md: str) -> dict[str, Any]:
    """Convert Markdown to structurally rich ADF (headings, lists, code, marks).

    Falls back to the plain :func:`text_to_adf` encode when the ``marklas`` extra is
    absent, when conversion raises, or when the conversion would DROP an HTML
    comment. That last case is a real content-loss defect, not a cosmetic one:
    marklas removes HTML comments (``text <!-- x --> more`` becomes ``text  more``,
    and a lone marker becomes the empty string), and rebar's own
    ``<!-- rebar:reconciler-echo -->`` marker is an HTML comment. So the encode
    verifies itself by decoding its own result and comparing the comments present in
    the source; a body that would lose one degrades to the plain encode, trading
    richness for content preservation.
    """
    engine = _marklas()
    if engine is None or not isinstance(md, str):
        return text_to_adf(md)
    try:
        doc = _canonicalize_marks(engine.to_adf(md))
        comments = _HTML_COMMENT_RE.findall(md)
        if comments:
            decoded = engine.to_md(doc)
            if any(comment not in decoded for comment in comments):
                return text_to_adf(md)
    except Exception:  # noqa: BLE001 - any engine failure degrades, never propagates
        return text_to_adf(md)
    return dict(doc)


def adf_to_markdown(doc: dict[str, Any] | None) -> str:
    """Convert an ADF document back to Markdown, canonicalizing mark order.

    Falls back to the plain :func:`adf_to_text` decode when the ``marklas`` extra is
    absent or conversion raises.
    """
    engine = _marklas()
    if engine is None or not isinstance(doc, dict):
        return adf_to_text(doc)
    try:
        return str(engine.to_md(_canonicalize_marks(doc)))
    except Exception:  # noqa: BLE001 - any engine failure degrades, never propagates
        return adf_to_text(doc)


def fit_markdown_to_adf_limit(md: str, *, limit: int = _ADF_DESCRIPTION_LIMIT) -> str:
    """Truncate ``md`` so its MARKDOWN-AWARE ADF serialization fits ``limit``.

    The plain :func:`fit_text_to_adf_limit` measures ``text_to_adf``, which inflates
    differently from ``markdown_to_adf`` — so a Markdown-aware wire fitted with the
    plain function can still exceed the cap. Same binary search, measuring the real
    document. Idempotent: a value already within ``limit`` is returned unchanged.
    """
    if not isinstance(md, str) or len(json.dumps(markdown_to_adf(md))) <= limit:
        return md
    lo, hi, best = 0, len(md), _ADF_TRUNCATION_SUFFIX
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = md[:mid] + _ADF_TRUNCATION_SUFFIX
        if len(json.dumps(markdown_to_adf(candidate))) <= limit:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def plain_text_adf_functions() -> dict[str, Any]:
    """Return the PLAIN (non-Markdown-aware) conversion functions as a set.

    Story 3388 installs this whole-codec fallback when a conversion error makes the
    Markdown-aware path untrustworthy: swapping the set atomically keeps encode,
    decode and fit consistent with one another, which a per-call fallback cannot
    guarantee. Defined and unit-tested here; wired there.
    """
    return {
        "to_adf": text_to_adf,
        "to_text": adf_to_text,
        "fit": fit_text_to_adf_limit,
        "normalize": normalize_description,
    }
