"""Full-text search over reduced ticket states (single source of truth).

Extracted verbatim from ``ticket-search.py`` so the CLI script and the in-process
library share ONE matching implementation (recommendation-#2 Step 1). Operates on
the raw reduced-state dicts returned by ``reduce_all_tickets``; presentation
shaping (``public_state``) stays the caller's concern, preserving the existing
``search → public_state`` order.
"""

from __future__ import annotations


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
) -> list[dict]:
    """Return the subset of ``states`` matching ``query`` (AND over
    whitespace-split, case-insensitive terms) and the optional
    status/type/tag filters. Error dicts (no ``status`` key) are skipped."""
    terms = [t for t in query.lower().split() if t]
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
        if all(term in hay for term in terms):
            out.append(st)
    return out
