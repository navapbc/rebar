"""Store-derived overlap context for plan review (a350-2e7e).

Each plan is read in isolation, so two tickets planning one change both pass. This module
retrieves the tickets whose recorded plans most resemble the one under review and renders
them as finder context. It rides the index the create-time surfacing story already built:
there is no second retrieval path, and no LLM call is added — the candidates travel inside
a Pass-1 chunk that is already being sent.

Retrieval lives here rather than in ``pass1`` because that module is near the 800-line cap;
``pass1`` gains only the call.
"""

from __future__ import annotations

from typing import Any

from rebar.similar import RESULT_LIMIT, find_similar

# The criterion the candidates are injected for. Fan-in is what puts this id in a chunk's
# rubric, so keying the injection on the id keeps the off switch total: not fanned in, the
# id reaches no chunk, and nothing here ever runs.
CRITERION_ID = "project.plan-overlap"

HEADING = "## POSSIBLE OVERLAP (from store)"

_DIRECTIVE = (
    "The tickets below have the plans most similar to this one. Judge whether THIS plan "
    "does work a listed ticket already describes. Similar subject matter is not overlap: "
    "flag only where the two plans would produce the same change. Name the ticket you "
    "mean. If none overlaps, say nothing."
)


def overlap_candidates(
    ctx: Any, coverage: dict[str, Any], *, limit: int = RESULT_LIMIT
) -> list[dict[str, Any]]:
    """Return the candidate tickets, recording the retrieval status in ``coverage``.

    ``limit`` defaults to the index's own ``RESULT_LIMIT``. ADR 0122 makes k=5 load-bearing
    for this index, so overlap keeps that operating point rather than choosing its own.

    On any non-``ok`` status the candidate list is empty and the review proceeds normally:
    the point of the status is that a degraded retrieval is DISTINGUISHABLE from an honest
    empty match, not that it stops the review.
    """
    diagnostics: dict[str, Any] = {}
    candidates = find_similar(
        ctx.tickets_root or "",
        text=ctx.plan_text,
        exclude_id=ctx.ticket_id,
        limit=limit,
        diagnostics=diagnostics,
    )
    status = diagnostics.get("status", "retrieval-error")
    coverage["overlap"] = {"status": status, "candidates": len(candidates) if candidates else 0}
    if status != "ok":
        return []
    return candidates


def overlap_context_block(candidates: list[dict[str, Any]]) -> str:
    """Render the candidates as finder context, or ``""`` when there are none.

    An empty block is not injected at all: the injected text is hashed into the chunk's
    ``context_digest``, so emitting a heading with no candidates would change checkpoint
    identity while carrying no information.

    Only the keys retrieval actually produces are rendered. Both backends —
    ``lexical.search`` and ``embedding.rank`` — return exactly ``id``/``alias``/``title``/
    ``score``, so a status field could only ever render as a constant placeholder, and a
    candidate's identity has to fall back to ``id`` rather than to a key no backend emits.
    ``score`` is rendered because it is the one signal that separates a near-duplicate plan
    from a distant neighbour, and without it every candidate looks equally implicated.
    """
    if not candidates:
        return ""
    lines = [HEADING, _DIRECTIVE]
    for c in candidates:
        ident = c.get("alias") or c.get("id") or "?"
        title = (c.get("title") or "").strip()[:80]
        try:
            score = f"{float(c.get('score', 0.0)):.2f}"
        except (TypeError, ValueError):
            score = "?"
        lines.append(f"  - {ident} -- {title} (similarity {score})")
    return "\n".join(lines)


def overlap_block(ctx: Any, coverage: dict[str, Any], *, limit: int = RESULT_LIMIT) -> str:
    """Retrieve and render in one call — what ``pass1`` uses."""
    return overlap_context_block(overlap_candidates(ctx, coverage, limit=limit))
