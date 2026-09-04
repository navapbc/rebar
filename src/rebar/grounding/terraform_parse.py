"""The pure, in-worker Terraform structural parse (REB-640, slice
forcible-diminished-lamb).

This module is the ONLY place ``python-hcl2`` is touched. Everything here is
importable in the leanest install: ``hcl2``/``lark`` are imported LAZILY inside
:func:`parse_document`, which is the single module-level callable the grounding
worker boundary (:func:`rebar.grounding.harness.run_in_worker`) invokes in a spawn
subprocess — so a hang/segfault in the C-ish parser becomes a fail-open abstain,
never a host crash.

:func:`parse_document` returns a PLAIN, pickleable dict of STRUCTURAL facts only —
block kinds, canonical addresses, definition-site spans, and module-call sources.
It NEVER returns attribute literal values or ``default`` values (redaction is
structural: those are simply not collected), so a receipt/evidence record built
from it cannot leak a credential.
"""

from __future__ import annotations

from typing import Any

#: Marker returned by :func:`parse_document` when the bytes could not be decoded
#: as strict UTF-8 (a pre-worker/undecodable-input condition → parse_error).
DECODE_ERROR = "__decode_error__"

# The HCL block types this slice addresses, mapped to their closed terraform_kind.
_SIMPLE_KINDS: dict[str, str] = {
    "variable": "variable",
    "output": "output",
    "module": "module",
    "provider": "provider",
}


def _strip_quotes(text: str) -> str:
    """Strip one layer of HCL string quoting from a loads() key/value."""
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _block_spans(text: str) -> list[tuple[str, tuple[int, int]]]:
    """Ordered ``(block_type, (line_start, line_end))`` for every real block.

    Uses ``parses_to_tree`` + public Lark ``Tree.meta`` (1-based) for spans; the
    v8.1.3 serialized metadata does NOT carry the line keys (ticket evidence
    2026-09-03), so the tree is the only span source.
    """
    import hcl2  # lazy: keeps `import rebar` lean (lean-import oracle)
    import lark

    tree = hcl2.parses_to_tree(text)
    spans: list[tuple[str, tuple[int, int]]] = []
    body = tree.children[0]
    for blk in body.children:
        if not isinstance(blk, lark.Tree) or blk.data != "block":
            continue
        btype = None
        for child in blk.children:
            if isinstance(child, lark.Tree) and child.data == "identifier":
                btype = str(child.children[0])
                break
        if btype is None:
            continue
        meta = blk.meta
        spans.append((btype, (int(meta.line), int(meta.end_line))))
    return spans


def _managed_or_data(btype: str, entry: dict[str, Any]) -> list[tuple[str, str]]:
    """Yield ``(terraform_kind, address)`` for a resource/data block entry.

    A ``resource``/``data`` block nests ``{type: {name: {...}}}``; the address is
    ``<type>.<name>`` (managed) or ``data.<type>.<name>`` (data).
    """
    out: list[tuple[str, str]] = []
    for raw_type, names in entry.items():
        rtype = _strip_quotes(raw_type)
        if not isinstance(names, dict):
            continue
        for raw_name in names:
            name = _strip_quotes(raw_name)
            if btype == "data":
                out.append(("data_resource", f"data.{rtype}.{name}"))
            else:
                out.append(("managed_resource", f"{rtype}.{name}"))
    return out


def _declarations(text: str) -> list[dict[str, Any]]:
    """Structural declarations (kind, address, span) correlated by document order."""
    import hcl2  # lazy

    data = hcl2.loads(text)
    spans = _block_spans(text)
    counters: dict[str, int] = {}
    decls: list[dict[str, Any]] = []
    for btype, span in spans:
        idx = counters.get(btype, 0)
        counters[btype] = idx + 1
        entries = data.get(btype)
        if not isinstance(entries, list) or idx >= len(entries):
            continue
        entry = entries[idx]
        if not isinstance(entry, dict):
            continue
        for kind, address in _addresses_for(btype, entry):
            decls.append(
                {"kind": kind, "address": address, "line_start": span[0], "line_end": span[1]}
            )
    return decls


def _addresses_for(btype: str, entry: dict[str, Any]) -> list[tuple[str, str]]:
    """Map one block's loads() entry to its ``(terraform_kind, address)`` pairs."""
    if btype in _SIMPLE_KINDS:
        kind = _SIMPLE_KINDS[btype]
        for raw_label in entry:
            if raw_label == "__is_block__":
                continue
            return [(kind, f"{btype}.{_strip_quotes(raw_label)}")]
        return []
    if btype == "locals":
        return [("local", f"local.{name}") for name in entry if name != "__is_block__"]
    if btype in ("resource", "data"):
        return _managed_or_data(btype, entry)
    return []


def _module_calls(text: str) -> list[dict[str, Any]]:
    """The ``module`` block calls: ``{name, source, dynamic}`` (source redaction-safe)."""
    import hcl2  # lazy

    data = hcl2.loads(text)
    calls: list[dict[str, Any]] = []
    for entry in data.get("module", []) or []:
        if not isinstance(entry, dict):
            continue
        for raw_name, body in entry.items():
            if raw_name == "__is_block__" or not isinstance(body, dict):
                continue
            raw_source = body.get("source")
            if isinstance(raw_source, str) and "${" not in raw_source:
                source: str | None = _strip_quotes(raw_source)
                dynamic = False
            else:
                source = None
                dynamic = True
            calls.append({"name": _strip_quotes(raw_name), "source": source, "dynamic": dynamic})
    return calls


def parse_document(text: str) -> dict[str, Any]:
    """Parse one Terraform document to a plain, pickleable STRUCTURAL fact dict.

    The single callable the worker boundary invokes. Returns
    ``{"declarations": [...], "module_calls": [...]}``; declarations carry only a
    closed ``kind``, a canonical ``address``, and a 1-based ``line_start``/
    ``line_end`` — never a literal value. Raises are the worker's concern (a
    syntax error propagates so the harness maps it to a fail-open abstain).
    """
    return {"declarations": _declarations(text), "module_calls": _module_calls(text)}


def parse_document_safe(text: str) -> dict[str, Any]:
    """Worker entry: :func:`parse_document`, but a parse/syntax error is CAUGHT and
    returned as a sentinel rather than raised.

    Runs inside :func:`rebar.grounding.harness.run_in_worker`. A raised parse/syntax
    error here would reach the harness as a generic ``other``; catching it and
    returning ``{"ok": False, "error": "parse_error"}`` lets the caller map a
    genuine HCL syntax error to the closed ``parse_error/invalid_input`` abstention
    (a hang/segfault still fails open through the harness).
    """
    try:
        return {"ok": True, **parse_document(text)}
    except Exception as exc:  # noqa: BLE001 — a syntax/parse error is a fail-open parse_error
        return {"ok": False, "error": "parse_error", "detail": type(exc).__name__}
