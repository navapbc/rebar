"""Structured-query parser for ``search`` (P1.1).

Turns a query string into a flat AND of *predicates* and *free-text terms*,
each optionally negated. There is **no bare ``OR`` keyword** — the only OR
mechanism is comma within a field value (``status:open,in_progress``), matching
GitHub issue-search and rebar's existing CSV filters. A literal ``OR`` token
therefore degrades to a free-text substring like any other word.

Grammar (whitespace-tokenized, case-insensitive free text):

  * ``field:value`` for a KNOWN field (``status``/``type``/``priority``/
    ``assignee``/``tag``/``parent``) becomes a predicate. Comma in the value is
    OR (``op=in``); ``priority`` additionally accepts the GitHub range operators
    ``<``/``<=``/``>``/``>=`` and ``n..m`` / ``*..n`` / ``n..*`` ranges.
  * Leading ``-`` or ``not:`` negates the token (``-status:closed``, ``-login``).
  * An UNKNOWN ``field:value`` (or any plain word) is a literal substring term —
    no crash, no special meaning. This is what keeps a predicate-free query
    byte-identical to the historical whitespace-AND substring search.

The matcher lives in :func:`rebar.reducer._filters.match_predicate`; this module
only parses. Returned shapes:

  predicates: list[tuple[field:str, op:str, value, negate:bool]]
  text_terms: list[tuple[substring:str, negate:bool]]
"""

from __future__ import annotations

KNOWN_FIELDS = frozenset({"status", "type", "priority", "assignee", "tag", "parent"})


def _parse_value(field: str, raw: str) -> tuple[str, object]:
    """Map a raw field value to ``(op, value)``. ``priority`` understands range
    operators; every field understands comma-OR; otherwise it is exact (``eq``)."""
    if field == "priority":
        if "," in raw:
            return ("in", {v for v in raw.split(",") if v})
        if ".." in raw:
            raw_lo, raw_hi = raw.split("..", 1)
            # An empty or ``*`` bound is open-ended (GitHub ``*..n`` / ``n..*``).
            lo = None if raw_lo in ("", "*") else raw_lo
            hi = None if raw_hi in ("", "*") else raw_hi
            return ("range", (lo, hi))
        for sym, op in (("<=", "le"), (">=", "ge"), ("<", "lt"), (">", "gt")):
            if raw.startswith(sym):
                return (op, raw[len(sym) :])
        return ("eq", raw)
    if "," in raw:
        return ("in", {v for v in raw.split(",") if v})
    return ("eq", raw)


def parse_query(query: str) -> tuple[list[tuple], list[tuple]]:
    """Parse ``query`` into ``(predicates, text_terms)``.

    A predicate-free query yields an empty ``predicates`` list and one lowercased
    substring term per whitespace token — identical to the historical search.
    """
    predicates: list[tuple] = []
    text_terms: list[tuple] = []
    for token in query.split():
        negate = False
        tok = token
        if tok.startswith("not:"):
            negate, tok = True, tok[4:]
        elif tok.startswith("-") and len(tok) > 1:
            negate, tok = True, tok[1:]

        field, sep, raw = tok.partition(":")
        if sep and raw != "" and field.lower() in KNOWN_FIELDS:
            fld = field.lower()
            op, value = _parse_value(fld, raw)
            predicates.append((fld, op, value, negate))
            continue

        # Plain word OR unknown field:value -> literal substring (degrade rule).
        text_terms.append((tok.lower(), negate))
    return predicates, text_terms
