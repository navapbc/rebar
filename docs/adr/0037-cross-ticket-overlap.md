# ADR 0037 — Store-wide cross-ticket overlap detection (Cupid digests, no embeddings)

**Status:** Accepted (epic only-crave-art — cross-ticket overlap & dependency detection)
**Date:** 2026-07-09

## Context

The plan-review gate reviews ONE ticket's plan in isolation. It deliberately does not detect
SEMANTIC overlap, duplication, or a hidden cross-ticket dependency BETWEEN separate tickets.
Existing coverage is partial and different in kind: `G4` checks consistency within one epic's
children; `next_batch` schedules around DETERMINISTIC file-impact conflicts; `duplicates` /
`supersedes` links are manual. The residual — semantic overlap across UNRELATED tickets
(cross-epic and non-epic) — was uncovered.

## Decision

Add a **store-wide, advisory-only** overlap detector that runs during the plan-review
invocation window (before claim), comparing the ticket under review against the whole store.
It NEVER affects the claim-gate verdict — it surfaces at most three candidate
duplicate/supersede/dependency link suggestions, each a ready-to-run `rebar link` command, for
human confirmation.

Four load-bearing choices, each derisked by a measured experiment (2026-07-09):

1. **Cupid digests WITHOUT embeddings** (arXiv:2308.10022). Each ticket gets a compact,
   LLM-extracted digest `{problem_keywords, component_or_area, key_entities, propositions}`;
   logs/traces/code are discarded. Digest-BM25F retrieved 8/8 heavy paraphrases at rank 1
   (recall@20 = 1.0), STABLE from a 60- to a 200-ticket corpus, where raw-BM25 degraded from
   0.875 → 0.75 as distractors grew. So the digest — not an embedding — is the load-bearing
   artifact, keeping rebar on its Anthropic-only, no-heavy-ML grain (no vector DB, no new
   dependency).

2. **Brute-force BM25F, not an ANN index.** Field-weighted BM25F over a few hundred digests is
   a single-query 0.69 ms @ 800 digests (~14× under a 10 ms budget); the unanimous research
   verdict is that ANN pays off only >100K vectors — we are 100-1000× below that. A bespoke
   ~40-LOC scorer avoids a dependency.

3. **The event store IS the enrichment queue.** Enrichment is amortized-cheap
   (~$0.0024/ticket, ≈ $1.90 one-time for 793 tickets, cached and re-run only on edit) and
   must run async off the hot write path. Rather than a bolted-on SQLite/Redis/broker (which
   could not live on the shared tickets path nor be visible to all clones), we model a
   broker-less queue as reducer-ignored sidecar events (`ENQUEUE_ENRICH` / `CLAIM_ENRICH` /
   `DONE_ENRICH`): cert-triggered enqueue with a stored-timestamp soak (debounce, not an
   in-process timer), latest-wins reduce, and an optimistic claim + lease (self-healing, no
   reaper). Drained opportunistically (Tier-1 detached drain, git-gc-auto style); Tier-2/3
   are spun out as ideas.

4. **The digest is a REVIEW_RESULT-style sidecar, and overlap runs at review time — NOT as a
   `5fd2` per-ticket criterion.** A per-ticket reviewer cannot see the whole store, which is
   exactly why this is a separate STORE-WIDE step in the same window with wider data scope. The
   `TICKET_DIGEST` record is content-hash-keyed and reducer-ignored, so it never enters
   compiled state / deps / validate / claim / close hot paths; freshness is computed on read
   (content / model / hash-version), fail-closed. The overlap results ride in a SEPARATE
   `overlap[]` verdict key added AFTER `sidecar.emit` + signing, so the sidecar, coverage
   counts, and attestation are byte-identical whether overlap is on or off.

## Consequences

- **Advisory only, suggestion-not-mutation.** A false suggestion costs one ignored line; a
  false auto-link would silently corrupt the dependency graph that `ready` / `next_batch` /
  the completion gate depend on — so the tool NEVER auto-links. Precision lives in the Stage-2
  pairwise judge (both orderings, a required cited shared artifact, default related-but-distinct,
  abstain-when-unsure), directed so the emitted `rebar link` command is never inverted.
- **Gated off by default** (`verify.overlap_enabled`); all tunables live on `LLMConfig`
  (`[tool.rebar.llm]`, `REBAR_LLM_OVERLAP_*`).
- **Graceful skip, no fallback.** If the `[agents]` extra / plan-review / an API key is absent,
  the whole feature silently no-ops. A cold store surfaces little until drains warm the digest
  cache — acceptable for an advisory aid.
