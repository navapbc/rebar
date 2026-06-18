"""Full-text search over reduced ticket states (single source of truth).

Extracted verbatim from ``ticket-search.py`` so the CLI script and the in-process
library share ONE matching implementation (recommendation-#2 Step 1). Operates on
the raw reduced-state dicts returned by ``reduce_all_tickets``; presentation
shaping (``public_state``) stays the caller's concern, preserving the existing
``search → public_state`` order.
"""

from __future__ import annotations

from collections.abc import Callable

from rebar.reducer._filters import match_predicate
from rebar.reducer._query import parse_query


def _haystack(state: dict) -> str:
    parts = [
        str(state.get("title") or ""),
        str(state.get("description") or ""),
        " ".join(str(t) for t in (state.get("tags") or [])),
    ]
    for c in state.get("comments") or []:
        if isinstance(c, dict):
            parts.append(str(c.get("body") or ""))
        else:
            parts.append(str(c))
    return "\n".join(parts).lower()


def search_states(
    states: list[dict],
    query: str,
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    has_tag: str | None = None,
    parent_resolver: Callable[[str], str] | None = None,
) -> list[dict]:
    """Return the subset of ``states`` matching ``query`` and the optional
    status/type/tag filters. Error dicts (no ``status`` key) are skipped.

    ``query`` is parsed by :func:`rebar.reducer._query.parse_query` into field
    predicates (comma-OR, ``priority`` ranges, ``-``/``not:`` negation) plus
    free-text substring terms; a predicate-free query reduces to the historical
    whitespace-AND substring search (byte-for-byte). The ``status``/
    ``ticket_type``/``has_tag`` keyword filters AND-narrow on top of any
    in-query predicate (no override either way).

    ``parent_resolver``, when supplied, maps a ``parent:`` predicate value
    (which may be an alias) to a canonical ticket id before matching — the
    resolution the decision record requires. Without it, ``parent:`` matches the
    raw value verbatim (fine for full-id inputs / unit tests)."""
    predicates, text_terms = parse_query(query)
    if parent_resolver is not None:
        predicates = [
            (f, op, parent_resolver(value) if f == "parent" and op == "eq" else value, neg)
            for (f, op, value, neg) in predicates
        ]
    out = []
    for st in states:
        if not isinstance(st, dict) or "status" not in st:
            continue  # skip error dicts
        if status is not None and st.get("status") != status:
            continue
        if ticket_type is not None and st.get("ticket_type") != ticket_type:
            continue
        if has_tag is not None and has_tag not in (st.get("tags") or []):
            continue
        hay = _haystack(st)
        # A term/predicate matches the ticket iff its presence != its negate flag.
        if any((term in hay) == neg for term, neg in text_terms):
            continue
        if any(match_predicate(st, f, op, val) == neg for f, op, val, neg in predicates):
            continue
        out.append(st)
    return out
