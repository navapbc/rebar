"""Stage-1 BM25F candidate generation over ticket digests (epic only-crave-art, 5a8f).

A bespoke field-weighted BM25 ("More Like This") over cached Cupid digests — brute-force
over a few hundred docs (sub-10 ms; the derisking experiment measured 0.69 ms @ 800), so
NO ANN / vector DB (unanimous research verdict: ANN only pays off >100K vectors, ~100-1000x
above our scale). ``rank-bm25`` provides only UNWEIGHTED BM25; per-field weighting is a
trivial ~40-LOC bespoke scorer that avoids a dependency.

The query side is the query ticket's freshly-extracted digest; the corpus side is the
cached digests of every other ticket. Graph exclusion (``graph.related_ticket_ids``) is the
caller's responsibility, passed as ``exclude``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from rebar.llm.config import LLMConfig

# Field weights are a stable ALGORITHMIC constant, deliberately NOT a config knob (a
# dict-typed config value has no from_env resolution helper, and BM25F weights do not need
# per-invocation tuning). Title/problem-statement fields weighted high per the design.
_FIELD_WEIGHTS: dict[str, float] = {
    "problem_keywords": 3.0,
    "component_or_area": 3.0,
    "key_entities": 1.0,
    "propositions": 1.0,
}

# BM25 saturation / length-normalization parameters (standard defaults).
_K1 = 1.5
_B = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# A small stopword set — enough to drop the most common English filler + digest boilerplate
# without a dependency. (Corpus-frequency pruning via max_doc_freq handles the rest.)
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "be",
        "with",
        "by",
        "as",
        "at",
        "it",
        "this",
        "that",
        "these",
        "those",
        "not",
        "no",
        "we",
        "our",
        "you",
        "your",
        "so",
        "but",
        "if",
        "then",
        "than",
        "into",
        "over",
        "must",
        "should",
        "can",
        "cannot",
        "will",
        "when",
        "where",
        "which",
        "who",
        "via",
    }
)


@dataclass
class OverlapCandidate:
    """A Stage-1 retrieval hit: a corpus ticket that lexically resembles the query digest."""

    ticket_id: str
    score: float
    matched_terms: list[str] = field(default_factory=list)


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS]


def _field_texts(digest: dict) -> dict[str, list[str]]:
    """Tokenize each weighted field of a digest. List fields join their items; the string
    ``component_or_area`` is tokenized directly."""
    out: dict[str, list[str]] = {}
    for fieldname in _FIELD_WEIGHTS:
        value = digest.get(fieldname)
        if isinstance(value, list):
            text = " ".join(str(v) for v in value)
        else:
            text = str(value or "")
        out[fieldname] = _tokenize(text)
    return out


def _weighted_tf(digest: dict) -> dict[str, float]:
    """The field-weighted term frequency of a digest: each field's term counts scaled by its
    weight, summed across fields (BM25F term boosting)."""
    wtf: dict[str, float] = {}
    for fieldname, tokens in _field_texts(digest).items():
        weight = _FIELD_WEIGHTS[fieldname]
        for term, count in Counter(tokens).items():
            wtf[term] = wtf.get(term, 0.0) + weight * count
    return wtf


def build_corpus(tracker: str, *, repo_root=None) -> dict[str, dict]:
    """Assemble the ``{ticket_id: digest}`` corpus from the store, including ONLY tickets
    whose cached digest is PRESENT AND FRESH (absent/stale digests are skipped, and the
    skip count is logged so a cold store is distinguishable from a healthy one). This is the
    corpus-side input to :func:`retrieve`."""
    import logging

    from rebar._engine_support.descendants import _load_all_states
    from rebar.llm.overlap import digest_sidecar

    corpus: dict[str, dict] = {}
    skipped = 0
    for tid in _load_all_states(tracker):
        payload = digest_sidecar.latest_ticket_digest(tid, tracker=tracker, repo_root=repo_root)
        if (
            payload is not None
            and isinstance(payload.get("digest"), dict)
            and digest_sidecar.freshness(tid, tracker=tracker, repo_root=repo_root)
            == "present-fresh"
        ):
            corpus[tid] = payload["digest"]
        else:
            skipped += 1
    logging.getLogger(__name__).info(
        "overlap corpus: %d fresh digests, %d skipped (absent/stale)", len(corpus), skipped
    )
    return corpus


def retrieve(
    query_digest: dict,
    corpus: dict[str, dict],
    exclude: set[str],
    *,
    config: LLMConfig | None = None,
) -> list[OverlapCandidate]:
    """Return the top-K ``OverlapCandidate``s for ``query_digest`` by field-weighted BM25F
    over ``corpus`` (a ``{ticket_id: digest}`` map), excluding ids in ``exclude``.

    Boilerplate terms (appearing in > ``overlap_max_doc_freq`` of the corpus) are pruned; a
    candidate must share at least ``overlap_min_should_match`` of the query's distinct terms
    to be returned (else the floor yields ``[]``). Returns ``[]`` on any scoring error — this
    is advisory retrieval and must never block the caller."""
    try:
        cfg = config or LLMConfig.from_env()
        docs = {
            tid: dg for tid, dg in corpus.items() if tid not in exclude and isinstance(dg, dict)
        }
        n = len(docs)
        if n == 0:
            return []

        # Per-doc weighted term frequencies + document lengths; document frequency per term.
        doc_wtf: dict[str, dict[str, float]] = {}
        doc_len: dict[str, float] = {}
        df: Counter = Counter()
        for tid, dg in docs.items():
            wtf = _weighted_tf(dg)
            doc_wtf[tid] = wtf
            doc_len[tid] = sum(wtf.values())
            for term in wtf:
                df[term] += 1

        avgdl = (sum(doc_len.values()) / n) or 1.0

        # Boilerplate prune: terms appearing in > max_doc_freq of the corpus carry no signal.
        boilerplate = {t for t, c in df.items() if c / n > cfg.overlap_max_doc_freq}

        query_terms = [t for t in _weighted_tf(query_digest) if t not in boilerplate]
        if not query_terms:
            return []
        q_distinct = set(query_terms)
        needed = max(1, math.ceil(cfg.overlap_min_should_match * len(q_distinct)))

        idf = {t: math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_distinct if t in df}

        candidates: list[OverlapCandidate] = []
        for tid, wtf in doc_wtf.items():
            matched = [t for t in q_distinct if t in wtf and t in idf]
            if len(matched) < needed:
                continue
            dl = doc_len[tid]
            score = 0.0
            for t in matched:
                f = wtf[t]
                denom = f + _K1 * (1.0 - _B + _B * dl / avgdl)
                score += idf[t] * (f * (_K1 + 1.0)) / denom if denom else 0.0
            if score > 0.0:
                candidates.append(
                    OverlapCandidate(ticket_id=tid, score=score, matched_terms=sorted(matched))
                )

        # Deterministic order: score desc, then ticket_id for stable ties.
        candidates.sort(key=lambda c: (-c.score, c.ticket_id))
        return candidates[: cfg.overlap_k]
    except Exception:  # noqa: BLE001 — advisory retrieval; never blocks the caller
        import logging

        logging.getLogger(__name__).warning("overlap retrieve failed; returning []", exc_info=True)
        return []
