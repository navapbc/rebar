"""Cross-ticket overlap detection (epic only-crave-art).

A store-wide, advisory-only detector that surfaces semantic overlap / duplication /
hidden dependency between UNRELATED tickets — the residual the per-ticket plan-review
gate cannot see. Two-stage retrieve-then-rerank over cached Cupid ticket digests
(no embeddings, no vector DB): the digest sidecar (this package) persists each ticket's
LLM-extracted digest; BM25F candidate generation narrows the corpus; a bounded LLM
pairwise judge classifies candidates into advisory link suggestions.
"""

from __future__ import annotations
