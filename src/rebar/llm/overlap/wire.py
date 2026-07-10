"""Wire the store-wide overlap step into plan review (epic only-crave-art, story 0f70).

``overlap_findings`` runs the full two-stage pipeline for one ticket — enrich (Cupid digest)
-> BM25F candidate generation (graph-excluded) -> bounded pairwise judge — and returns the
advisory link suggestions. It is ADVISORY ONLY: it never blocks a claim and never affects the
plan-review verdict; the caller folds the result into a SEPARATE ``overlap[]`` verdict key.

Graceful skip (no fallback): if the ``[agents]`` extra / an API key is absent, or anything in
the pipeline raises, or there are no candidates, it returns ``[]`` — a cold store simply
surfaces little until drains warm the digest cache.
"""

from __future__ import annotations

import logging

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner

logger = logging.getLogger(__name__)


def overlap_findings(
    ticket_id: str,
    *,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> list[dict]:
    """The store-wide overlap advisory findings for ``ticket_id`` (``[]`` on any skip/error)."""
    try:
        from rebar._commands._seam import tracker_dir
        from rebar.llm.enrich import enrich
        from rebar.llm.overlap import graph
        from rebar.llm.overlap import judge as judge_mod
        from rebar.llm.overlap import retrieve as retrieve_mod

        cfg = config or LLMConfig.from_env(repo_root=repo_root)
        tracker = str(tracker_dir(repo_root))

        # Stage 0: the query ticket's fresh digest (query side; corpus side is cached).
        query_digest = enrich(ticket_id=ticket_id, repo_root=repo_root, config=cfg, runner=runner)[
            "digest"
        ]

        # Stage 1: BM25F candidates over the fresh-digest corpus, excluding the query's own graph.
        corpus = retrieve_mod.build_corpus(tracker, repo_root=repo_root)
        exclude = graph.related_ticket_ids(ticket_id, tracker)
        exclude.add(ticket_id)
        candidates = retrieve_mod.retrieve(query_digest, corpus, exclude, config=cfg)
        if not candidates:
            return []

        # Stage 2: bounded pairwise judge → advisory link suggestions.
        return judge_mod.judge(
            ticket_id, query_digest, candidates, corpus, config=cfg, runner=runner
        )
    except Exception:  # noqa: BLE001 — advisory-only; a failure NEVER blocks or fails the review
        logger.warning("store-wide overlap step failed; no overlap findings", exc_info=True)
        return []
