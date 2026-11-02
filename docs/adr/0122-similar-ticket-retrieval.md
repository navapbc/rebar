# ADR 0122 — Similar-ticket retrieval: embed the LLM digest, surfaced at k=5

**Status:** Accepted
**Date:** 2026-10-31

> **Amended by task `b505-d66e-86b3-4446` (`weaponed-handheld-centipede`).** Three additions,
> none of which change the adopted candidate or the k=5 operating point: retrieval is recorded
> as **two-tiered** — a lexical FTS5 default needing no model provider, and this embedding tier
> as an **opt-in** upgrade; the embedding tier is recorded as **provider-agnostic**, with the
> benchmarked Bedrock pair named as the configuration measured rather than the one required;
> and the **measured latency** figures are reported, each with the conditions that produced it.
> The deployment precondition below is narrowed to constrain the embedding tier rather than the
> index as a whole.

## Context

Agents working this store repeatedly re-discover work that already has a ticket. `rebar search`
is a lexical full-text query, so it finds a prior ticket when the wording overlaps and misses it
when the same problem was described in different words. Epic 62dc asked for a measured answer to
one question: what retrieval architecture should surface similar tickets, and is a per-ticket LLM
call worth its cost?

This ADR records the measurement. Nothing here is projected or estimated; every number was
produced by `evals/similar-tickets/measure.py` and is reproducible by the quoted command.

### The fixture, and why it is stratified

`evals/similar-tickets/benchmark.json` is derived from the store's own recorded `duplicates`
links at tickets SHA `cad39ed1289ddb5859c490c2e01612ce1edc1420`: 630 tickets, 148 clusters,
482 `duplicates` links, 1,524 pairs. `measure.py --check` re-derives the pair set from the live
store at that SHA and compares it to the fixture exactly.

**The bulk-duplicate artifact is why a pooled figure is banned here.** Of the 1,524 pairs, 1,431
are `lexical` and only 93 are `semantic` — 93.9% lexical. That skew is not a property of
duplicate tickets in general; it is an artifact of how this store accumulated duplicates, with
large clusters of near-identically-worded tickets filed in bulk. Any pooled score is therefore
approximately a measurement of the lexical stratum alone, and would rank candidates by the
stratum the feature least needs help with. Every table below reports the two strata separately
and `print_table` prints that caveat on every run.

Each pair is scored in both directions, so the 1,431 lexical pairs give 2,862 directed queries
and the 93 semantic pairs give 186.

## Measurements

Three candidates were required by the plan. A fourth, `vector-raw`, was added as a diagnostic to
isolate what the LLM digest step contributes.

```sh
cd evals/similar-tickets
python measure.py --check                 # fixture re-derives from the live store
python measure.py --candidate fts5
python measure.py --candidate digest
python measure.py --candidate vector
python measure.py --candidate vector-raw  # diagnostic, not one of the three
```

| candidate | lexical r@5 | lexical r@10 | semantic r@5 | semantic r@10 |
| --- | --- | --- | --- | --- |
| `fts5` — sqlite FTS5, bm25 title 5.0 / description 1.0 | 0.771 | **0.993** | 0.823 | 0.952 |
| `digest` — BM25F over the LLM digest fields | 0.703 | 0.918 | 0.796 | 0.925 |
| `vector` — Titan v2 over the digest paragraph | 0.763 | 0.972 | **0.876** | **0.973** |
| `vector-raw` — Titan v2 over raw title+description | 0.765 | 0.977 | 0.839 | 0.946 |

Two results contradict the premises this work started from, and are recorded because they are
load-bearing for the decision:

- **Lexical retrieval does not fail the semantic stratum.** The plan premised that it would.
  `fts5` reaches 0.823 semantic r@5 and 0.952 semantic r@10 — a strong baseline that any
  paid architecture has to beat.
- **The digest BM25F candidate is the worst of the four on both strata.** Reducing a ticket to
  four digest fields and then searching those fields lexically discards more signal than the
  digest adds. It is not a candidate for adoption.

### The recall table alone cannot decide this

The semantic stratum has 186 directed queries. Differences of a few points there are inside
sampling noise, and reading two separately-produced tables side by side cannot tell a real
ordering from noise. The candidates answer the *same* queries, so the paired test is the one that
can, and the architecture choice rests on it rather than on the table.

```sh
python measure.py --paired           # McNemar at recall@5
python measure.py --paired --k 10    # McNemar at recall@10
```

Exact two-sided McNemar; only discordant queries carry information. **Semantic stratum:**

| comparison | k | A-only | B-only | p | verdict |
| --- | --- | --- | --- | --- | --- |
| `fts5` vs `vector` | 5 | 3 | 13 | 0.0213 | **vector distinguishably better** |
| `fts5` vs `vector-raw` | 5 | 9 | 12 | 0.6636 | not distinguishable |
| `vector` vs `vector-raw` | 5 | 9 | 2 | 0.0654 | not distinguishable |
| `fts5` vs `digest` | 5 | 12 | 7 | 0.3593 | not distinguishable |
| `digest` vs `vector` | 5 | 2 | 17 | 0.0007 | vector distinguishably better |
| `fts5` vs `vector` | 10 | 3 | 7 | 0.3438 | not distinguishable |
| `vector` vs `vector-raw` | 10 | 5 | 0 | 0.0625 | not distinguishable |

**Lexical stratum**, the comparisons that bear on the cost of adopting `vector`:

| comparison | k | A-only | B-only | p | verdict |
| --- | --- | --- | --- | --- | --- |
| `fts5` vs `vector` | 5 | 349 | 326 | 0.3971 | not distinguishable |
| `fts5` vs `vector` | 10 | 63 | 3 | 0.0000 | **fts5 distinguishably better** |
| `vector` vs `vector-raw` | 10 | 45 | 57 | 0.2760 | not distinguishable |

So `vector` has exactly one distinguishable advantage over the free baseline — semantic
recall@5 — and `fts5` has exactly one distinguishable advantage over `vector` — lexical
recall@10. At k=5 the lexical difference is noise; at k=10 the semantic difference is noise.
**The choice of architecture and the choice of operating point are the same decision.**

## Decision

**Adopt `vector`: embed the LLM digest paragraph and surface the top 5.**

Justified from the semantic stratum, as the epic requires: at k=5, `vector` is the only candidate
that distinguishably beats the zero-cost `fts5` baseline on the stratum the feature exists to
serve (p=0.0213, 13 queries recovered against 3 lost). `digest` is excluded as the worst
candidate on both strata. `fts5` is excluded because it is distinguishably worse on the semantic
stratum at the chosen operating point, which is the only stratum where a similar-ticket feature
earns its keep.

**k=5 is part of the decision, not an implementation detail.** At k=10 `vector`'s semantic
advantage disappears into noise (p=0.3438) while `fts5`'s lexical advantage becomes significant
(p=0.0000) — at that operating point the evidence favours keeping `fts5` and building nothing.
A future change to the surfaced count invalidates this ADR and must re-run `--paired`.

### Retrieval is two-tiered: a lexical default and an opt-in embedding upgrade

The measurement above compares retrieval quality. It does not by itself decide what runs in an
installation that has no model provider configured, and that is a separate decision this ADR
records explicitly, because `vector` cannot run there at all.

**The lexical FTS5 tier is the default, and it requires no model provider.** It needs no
credentials, no network and no configuration; it is available in every installation of rebar.
Its measured semantic recall@5 of 0.823 is the baseline every number above is quoted against,
so the default tier is not a degraded mode bolted on afterwards — it is the candidate `vector`
had to beat, shipped.

**The embedding tier is opt-in.** It is enabled by configuring an embedding model, and it
replaces only the scoring function over the same index, the same freshness key and the same
catch-up rule. When no model is configured, when the configured provider denies or times out,
or when the configured backend's optional dependency is not installed, retrieval returns the
lexical tier's results rather than failing.

This split is what keeps similar-ticket surfacing portable. A feature whose only path needs a
model provider would be unavailable to any installation without one; a feature whose default
path is lexical is available everywhere, and improves measurably where a provider exists. The
lexical tier is therefore a deliberate tier with its own measured number, not a fallback and
not dead code.

### The embedding tier is provider-agnostic

The models named throughout this ADR — `amazon.titan-embed-text-v2:0` for embedding and
`bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0` for the digest — are **the configuration
that was benchmarked**, named so the measurement is reproducible. They are not a property of
the architecture and not a requirement of adoption.

Both are resolved from configured provider-qualified model strings through
`pydantic_ai.embeddings.infer_embedding_model` and the equivalent chat-model seam rebar already
uses in `src/rebar/llm/providers.py`, so another provider's models are a configuration change
rather than a code change. rebar must run against a wide variety of providers and models, and
an architecture record that pinned one vendor's pair would misstate the system it governs.

What the measurement establishes is that *embedding a digest* beats lexical retrieval at k=5
on this fixture. It does not establish that any particular embedding model reproduces 0.876 —
a different model is a different measurement, and re-running `evals/similar-tickets/measure.py`
is what would settle it.

### Keeping the digest step is the weakest link, and is recorded as such

`vector` vs `vector-raw` is **not** distinguishable at either k (p=0.0654 at k=5, p=0.0625 at
k=10). Read alone, that says the per-ticket LLM call is unjustified. It is retained because
`vector-raw` *also* fails to distinguishably beat `fts5` on the semantic stratum (p=0.6636), so
dropping the digest step yields an architecture that pays for embedding infrastructure and
cannot be shown to buy anything over doing nothing.

This is a non-transitive chain — A beats C, B does not beat C, A does not beat B — and it is
stated plainly rather than presented as a clean result. Both digest comparisons sit just outside
α=0.05 in `vector`'s favour, on 11 and 5 discordant queries respectively. **The honest summary is
that the fixture's 93 semantic pairs are too few to resolve the digest step's value, and the
decision to keep it rests on the alternative being worse, not on the digest being proven.** If
the semantic stratum grows past roughly 300 pairs, re-run `--paired`; if `vector` vs `vector-raw`
is still not distinguishable, drop the digest step and embed raw text.

## Architecture

**Index path.** `.rebar/cache/similar-tickets/`, git-ignored under the existing `.rebar/*` rule,
alongside `.rebar/cache/plan-review/` (`src/rebar/llm/plan_review/checkpoints.py:33`). The index
is derived state: deleting it costs a rebuild and nothing else, and it is never committed.

**Freshness key.** Per ticket, `reducer._cache.compute_dir_hash(ticket_dir, event_filenames)`
(`src/rebar/reducer/_cache.py:122`) — the same hash rebar already uses to decide a ticket's
reduced state is unchanged. An entry holds the dir hash it was built from; the entry is valid
while that hash matches. No new freshness mechanism is introduced, and the key cannot drift from
the store's own notion of "this ticket changed", because it is that notion.

**Catch-up rule.** Each run diffs the index's recorded dir hashes against the live store and
processes only tickets whose hash changed or that have no entry; entries for deleted tickets are
dropped. Work is bounded by write volume since the last run, not by corpus size, so an index that
has fallen behind catches up incrementally without a full rebuild, and a cold start is just the
case where every ticket is missing.

**Embedding model.** Configured as a provider-qualified model string and resolved through
`infer_embedding_model`. The benchmarked configuration was `amazon.titan-embed-text-v2:0`,
1,024 dimensions, cosine similarity; cosine over the resulting vectors is the architecture, the
model is not.

**Digest.** Configured the same way; benchmarked with
`bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0`. Prompt pinned at commit
`f147e08242f`, input capped at 40,000 chars. Four fields — `propositions`, `key_entities`,
`problem_keywords`, `component_or_area` — concatenated into the paragraph that is embedded.
(The field weights 3.0 / 2.0 / 2.0 / 1.0 apply only to the rejected `digest` BM25F candidate.)

**Query.** Title plus the first 200 chars of the description.

### Measured latency, and why each figure carries its conditions

A bare millisecond figure is a property of the machine that produced it, not of the code. The
same store and the same code measured a warm-query p95 of roughly 100 ms on an idle developer
box and 545 ms on a shared CI runner — a 5x spread from hardware alone
(`tests/integration/test_similar_tickets_scale.py`). Every figure below is therefore stated with
the store size, the host, and the sample count, so a reader can tell what would have to be
re-measured on their own hardware rather than treating these as guarantees.

All four were produced by the lexical tier's scale test on a synthetic **10,000-ticket** store,
on the **idle macOS development host** the sibling index story was built on — not on CI:

| measurement | figure | samples |
| --- | --- | --- |
| cold catch-up p95 | 12,455 ms | n=5 |
| cold full rebuild | 16,940 ms | single run |
| warm query p95 | 120.8 ms | n=25 |
| ticket directories hashed per warm query, steady state | 0 | n=25 |

The cold figures are the price of building the index from nothing at that store size; the
catch-up rule means they are paid once, and thereafter work tracks write volume rather than
corpus size.

The zero in the last row is the only one of the four that is hardware-independent, and it is the
one the test asserts on. The warm p95 is asserted as a ratio against the cold rebuild measured in
the same run on the same host, so the hardware cancels; it is not asserted against an absolute
budget, because an earlier 500 ms budget that measured 107 ms locally measured 545 ms on
`macos-latest` and had to be replaced.

### Per-ticket cost

Measured over the 630-ticket corpus, with the benchmarked model pair. Another provider's models
carry their own token accounting and their own rates; the step structure is what this table
records, not the vendor:

| step | model | per ticket | corpus total |
| --- | --- | --- | --- |
| digest input | Haiku 4.5 | ~1,260 tokens (5,039 chars mean; 26,560 max) | ~794k tokens |
| digest output | Haiku 4.5 | ~300 tokens (1,201 chars mean) | ~189k tokens |
| embedding input | Titan v2 | ~300 tokens (the digest paragraph) | ~189k tokens |

One Haiku call and one Titan call per ticket, paid **once per ticket version** — the dir-hash key
means an unchanged ticket is never re-embedded, so steady-state cost tracks ticket writes, not
corpus size. Dropping the digest step would remove the Haiku line entirely and cut the Titan input
to the raw text; that saving is what the unresolved `vector` vs `vector-raw` comparison above is
weighed against. Token counts are given rather than dollars because the counts are measured and
durable while the rates are not; multiply by the current Bedrock rates at adoption time.

### Deployment precondition for the embedding tier

This section constrains the **embedding tier only**. The lexical default tier invokes no model,
so none of it applies there: an operator with no Bedrock grant, no credentials and no network
still gets similar-ticket retrieval.

The digest and the embedding run under **different Bedrock model families**, and the Gerrit
instance role did not originally admit both. `infra/terraform/iam_s7.tf` scoped
`bedrock:InvokeModel` to `anthropic.claude-*` ARNs only, so `amazon.titan-embed-text-v2:0`
was denied and the embedding call would have failed in a deployed environment.

That statement now also lists the `amazon.titan-embed-text-v2:0` foundation-model ARN, so the
grant covers both families. The widening is **declared, not confirmed live**, and landing it does
not apply it: applies are manual (`scripts/terraform_prod.py`), and the Terraform Drift workflow is
advisory, does not contribute to the Gerrit `Verified` label, and triggers on pull requests, a daily
sweep and manual dispatch — none of which a Gerrit change is. Bug `1c39-96d4-8f9e-4d38` records
that gap in general. A policy simulator cannot close it either, because Converse authorizes against
`bedrock:InvokeModel` while the distinct `bedrock:Converse` action simulates as denied. Only a real
call from the instance role proves access; `infra/runbooks/bedrock-access.md` §4 is the probe that
makes it.

Enabling the **embedding tier on this deployment** therefore depends on that grant being applied.
This is a precondition on that tier, not on the index: the index itself builds and serves under
the lexical tier with no grant at all, and no credentials beyond the developer's own are needed
to run it anywhere. The benchmark did not surface the gap because it ran under a local principal
that already had Titan access.

An installation configuring a **different provider** has a different precondition — whatever that
provider requires to authorize both the digest and the embedding call — and the Bedrock-specific
paragraphs above do not describe it.

## Consequences

- A similar-ticket feature built on this gains a semantic-stratum recall@5 of 0.876 against the
  0.823 the existing lexical search already achieves — a real but modest improvement, significant
  at k=5 and not at k=10.
- It costs an LLM call and an embedding call per ticket version, and a git-ignored index.
- It inherits the digest's failure modes. The digest parser silently returned empty fields for
  three distinct reply shapes — a `ticket_digest` wrapper key, YAML instead of JSON, and valid
  JSON behind a prose preamble — each indistinguishable from success without a failure counter.
  `measure.py` now reports an `empties` count on every digest build; any consumer of this
  architecture must keep that counter, because a lenient parser without one cannot tell mass
  failure from success.
- Two tickets in 630 did not answer the digest task at all (one began performing the ticket's
  refactoring work instead). Both succeeded on retry, so the failure is nondeterministic rather
  than a property of those tickets, and a retry is the correct handling.
- `fts5` remains distinguishably better for lexical recall@10 and is not removed. This ADR adds a
  retrieval path; it does not replace `rebar search`.
- Re-running `--paired` is required if the surfaced count changes or the semantic stratum grows.

## Alternatives rejected

**`digest` BM25F.** Worst of the four candidates on both strata (0.703/0.918 lexical,
0.796/0.925 semantic), distinguishably worse than `fts5` on lexical recall at both k. The digest
is useful as embedding input and not as a lexical index.

**`fts5` alone (build nothing).** Zero cost and the best lexical recall@10 in the set (0.993).
Rejected on the semantic stratum at k=5, where it is distinguishably worse than `vector`
(p=0.0213). This is the correct choice if the feature is ever changed to surface 10 results.

**`vector-raw` (drop the digest step).** Cheaper, and not distinguishably worse than `vector`.
Rejected because it is also not distinguishably better than `fts5` on the semantic stratum
(p=0.6636) — it pays for embedding infrastructure without a demonstrable gain over the free
baseline. Revisit when the semantic stratum is large enough to resolve the comparison.
