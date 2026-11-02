"""Lexical ranking over the FTS5 index -- ADR 0122's adopted retrieval tier.

This tier needs no model provider, no credentials and no network, which is why it is
the default: retrieval works in every installation, including one with no AI
configuration at all.

Ranking reproduces the ``fts5`` baseline ADR 0122 measured, deliberately and in
detail -- the query-text construction, the tokenization, and the bm25 weighting are
the measured ones, because a ranking that differs from the benchmarked one is not
the thing the ADR adopted.

Two details are worth stating outright:

**The result cap is 5, and that is part of the decision.** ADR 0122's adoption is
scoped to k=5. At k=10 the embedding candidate's advantage is statistically
indistinguishable (p=0.3438), which is the regime where the evidence favours
building nothing. Returning a different count would invalidate the comparison the
ADR rests on.

**``score`` is normalized, because raw bm25 cannot be handed to a consumer.** sqlite
returns bm25 as a NEGATIVE number ordered ascending -- the most relevant row is the
most negative. That inverts the conventional reading of a relevance score, and no
caller can write an assertion against it without knowing the sign convention. The
published ``score`` is therefore mapped onto 0..1 with 1 most relevant, by a
monotonic transform that preserves the bm25 ordering exactly.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from .index import BM25_WEIGHTS

logger = logging.getLogger(__name__)

#: ADR 0122's decision is scoped to k=5; see the module docstring.
RESULT_LIMIT = 5

#: Characters of the description that join the title to form the query text.
QUERY_DESC_HEAD = 200

#: Token cap, matching the measured baseline.
_MAX_TOKENS = 64

#: Word characters only. Punctuation is DROPPED rather than escaped: FTS5 treats
#: bare ``-``, ``*``, ``:`` and quotes as operators, so passing raw ticket prose to
#: MATCH raises a syntax error instead of returning results.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

#: Stands in for an empty token set so MATCH stays valid and simply finds nothing.
_NO_MATCH = '"zzzz_no_match"'


def query_text(title: str, description: str) -> str:
    """The query string for a ticket: its title plus the head of its description."""
    return f"{title or ''} {(description or '')[:QUERY_DESC_HEAD]}".strip()


def match_expression(text: str) -> str:
    """*text* as a safe FTS5 MATCH expression: quoted word tokens, OR-joined."""
    tokens = _TOKEN_RE.findall(text or "")
    return " OR ".join(f'"{t}"' for t in tokens[:_MAX_TOKENS]) or _NO_MATCH


def _score(bm25_value: float) -> float:
    """Map a raw bm25 value onto 0..1, 1 most relevant, preserving the ordering.

    bm25 is negative and ranks ascending, so relevance is its negation. ``r/(1+r)``
    is strictly increasing in ``r`` and bounded, so the best row keeps the highest
    score without the result set having to be consulted -- a single result is scored
    on its own merit rather than being forced to 1.0 by construction.
    """
    relevance = max(0.0, -float(bm25_value))
    return relevance / (1.0 + relevance)


def search(
    conn: sqlite3.Connection,
    text: str,
    *,
    exclude_id: str | None = None,
    limit: int = RESULT_LIMIT,
) -> list[dict]:
    """Ranked similar tickets for *text*, at most *limit* of them.

    *exclude_id* drops one ticket from the results. A create queries with its own new
    ticket's text, and catch-up has already indexed that ticket, so without the
    exclusion the caller gets itself back as the top match.

    A *limit* of zero or less asks for nothing and gets ``[]``. The truncation loop
    below runs after its first append, so without this guard a caller asking for zero
    results would be handed one.
    """
    if limit <= 0:
        return []
    expression = match_expression(text)
    # One extra row is fetched so the exclusion cannot shorten a full result set.
    rows = conn.execute(
        f"SELECT ticket_id, alias, title, bm25(docs, {BM25_WEIGHTS}) "
        f"FROM docs WHERE docs MATCH ? ORDER BY bm25(docs, {BM25_WEIGHTS}) LIMIT ?",
        (expression, max(1, limit) + 1),
    ).fetchall()

    results = []
    for ticket_id, alias, title, raw in rows:
        if exclude_id and ticket_id == exclude_id:
            continue
        results.append(
            {
                "id": ticket_id,
                "alias": alias or "",
                "title": title or "",
                "score": _score(raw),
            }
        )
        if len(results) >= limit:
            break
    return results
