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

SEARCH_PROJECTION_LIMIT = 240


def _searchable_parts(state: dict) -> list[str]:
    """Case-preserving fields in the historical full-text matching order."""
    parts = [
        str(state.get("title") or ""),
        str(state.get("description") or ""),
        " ".join(str(t) for t in (state.get("tags") or [])),
        # Identifiers a user is likely to paste into search: the canonical
        # ticket_id, the human alias, and the bound Jira key (folded in by the
        # caller — search_states stays pure). Lowercased with everything else by
        # the trailing ``.lower()``; matching stays the single substring path.
        str(state.get("ticket_id") or ""),
        str(state.get("alias") or ""),
        str(state.get("jira_key") or ""),
    ]
    for c in state.get("comments") or []:
        if isinstance(c, dict):
            parts.append(str(c.get("body") or ""))
        else:
            parts.append(str(c))
    return parts


def _searchable_text(state: dict) -> str:
    return "\n".join(_searchable_parts(state))


def _haystack(state: dict) -> str:
    """The historical lowercased search text, derived from case-preserving fields."""
    return _searchable_text(state).lower()


def _normalize_whitespace(value: object) -> str:
    return " ".join(str(value or "").split())


def _truncate(value: str, limit: int = SEARCH_PROJECTION_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _case_insensitive_span(text: str, term: str) -> tuple[int, int] | None:
    """Map a lowercase match back to its span in the case-preserving text.

    Unicode lowercase mappings can expand one source character into multiple
    comparison characters (for example, ``İ`` -> ``i`` + combining dot). The
    position map prevents those expansions from shifting a later snippet or
    splitting the expanded character when the term itself matches it.
    """
    lowered = text.lower()
    match_start = lowered.find(term)
    if match_start < 0:
        return None
    if len(lowered) == len(text):
        return match_start, len(term)

    source_indices = [index for index, char in enumerate(text) for _ in char.lower()]
    if len(source_indices) != len(lowered):  # pragma: no cover - defensive Unicode fallback
        return match_start, min(len(term), len(text) - match_start)
    match_end = match_start + len(term) - 1
    source_start = source_indices[match_start]
    source_end = source_indices[match_end] + 1
    return source_start, source_end - source_start


def _bounded_match_context(source: str, term: str) -> str | None:
    """Return normalized, case-preserving context centered on ``term``."""
    text = _normalize_whitespace(source)
    span = _case_insensitive_span(text, term)
    if span is None:
        return None
    match_start, match_len = span
    if len(text) <= SEARCH_PROJECTION_LIMIT:
        return text

    left_marker = right_marker = True
    start = end = 0
    # Marker presence affects the content budget. Recompute until the boundary
    # choice stabilizes (at most a few iterations for the two booleans).
    for _ in range(4):
        content_budget = SEARCH_PROJECTION_LIMIT - left_marker - right_marker
        surrounding = max(content_budget - match_len, 0)
        start = match_start - surrounding // 2
        start = max(0, min(start, len(text) - content_budget))
        end = min(len(text), start + content_budget)
        new_left, new_right = start > 0, end < len(text)
        if (new_left, new_right) == (left_marker, right_marker):
            break
        left_marker, right_marker = new_left, new_right

    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def _summary(state: dict) -> str | None:
    stored = _normalize_whitespace(state.get("summary"))
    fallback = _normalize_whitespace(state.get("description"))
    value = stored or fallback
    return _truncate(value) if value else None


def _snippet(state: dict, query: str) -> str | None:
    _predicates, text_terms = parse_query(query)
    positive_terms = [term for term, negated in text_terms if not negated]
    if not positive_terms:
        return None
    term = positive_terms[0]
    for part in _searchable_parts(state):
        snippet = _bounded_match_context(part, term)
        if snippet is not None:
            return snippet
    return None


def project_search_result(state: dict, query: str) -> dict:
    """Project full reduced state into the bounded discovery result contract."""
    return {
        "ticket_id": state.get("ticket_id"),
        "alias": state.get("alias"),
        "title": state.get("title"),
        "ticket_type": state.get("ticket_type"),
        "status": state.get("status"),
        "priority": state.get("priority"),
        "summary": _summary(state),
        "snippet": _snippet(state, query),
    }


def search_result_to_llm(result: dict) -> dict:
    """Minify one discovery result, omitting nullable fields when absent."""
    out = {
        "id": result["ticket_id"],
        "ttl": result["title"],
        "t": result["ticket_type"],
        "st": result["status"],
        "pr": result["priority"],
    }
    for source, target in (("alias", "a"), ("summary", "sm"), ("snippet", "sn")):
        value = result.get(source)
        if value is not None:
            out[target] = value
    return out


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
