# Similar-ticket retrieval benchmark

This directory measures how well a retrieval candidate finds a ticket's known
duplicates. It exists so the architecture recorded in
[ADR 0122](../../docs/adr/0122-similar-ticket-retrieval.md) rests on measured
recall rather than on an assumption about which approach ought to win.

The benchmark derives its ground truth from the store itself: every `duplicates`
link that operators have already recorded. Nothing here is hand-labelled.

## Files

| File | What it is |
|---|---|
| `measure.py` | The whole harness: fixture builder, verifier, and the three candidate runners. |
| `benchmark.json` | The committed fixture — the pair set and per-ticket identity (alias, title, description head), pinned to a tickets-branch SHA. |
| `corpus-text.json` | Full descriptions and digest inputs for the corpus. **Not committed** — 7.8 MB of ticket prose, rebuilt by `--build` alongside the fixture. `--check` never reads it. |
| `digests.json` | Cached LLM digests, one per corpus ticket. Committed so candidates 2 and 3 are reproducible with no model access. |
| `vectors-digest.json`, `vectors-raw.json` | Titan embedding caches for candidates 3 and 4. **Not committed** — 14 MB each, and regenerable from `digests.json` plus Bedrock access by re-running the candidate. |

## The fixture

`benchmark.json` records the tickets-branch SHA it was derived at. Every pair
carries both ticket ids, both aliases, both titles, both description heads, and a
stratum.

The fixture deliberately stops there. Full descriptions and digest inputs are
several megabytes and are a *derived* artifact: `--build` rebuilds them from the
store in the same pass that rebuilds the pair set, and `--check` re-derives from
the store rather than trusting any stored copy. They are written to the
uncommitted `corpus-text.json`, which the candidate runners load and `--check`
does not. Running a candidate without it fails with a message naming `--build`.

Pairs are **strata-tagged**, which is the point of the whole exercise:

- **`lexical`** — the two tickets share substantial wording. Most of these come
  from bulk-filed duplicate families, where many tickets were created from one
  template and differ by a few tokens.
- **`semantic`** — the two tickets describe the same problem in different words.

The split matters because the lexical stratum is large and easy, and pooling the
two hides how a candidate does on the cases that are actually hard. **Report per
stratum. Never report a pooled figure** — that is a requirement of the ADR, not a
stylistic preference.

### Rebuilding it

```sh
python evals/similar-tickets/measure.py --build
```

This reads the live store, walks the `duplicates` links into connected clusters,
and emits every within-cluster pair.

### Verifying it

```sh
python evals/similar-tickets/measure.py --check
```

Exit 0 means every id in the fixture still resolves in the store and the pair set
re-derives exactly at the recorded SHA. This is the command that keeps a
committed fixture honest; run it rather than trusting the file.

## Running a candidate

```sh
python evals/similar-tickets/measure.py --candidate fts5
python evals/similar-tickets/measure.py --candidate digest
python evals/similar-tickets/measure.py --candidate vector
```

Each prints recall@5 and recall@10 **per stratum**, with the query count behind
each figure. A candidate that cannot run reports `unavailable` and a reason
instead of a score, so a missing prerequisite can never be mistaken for a poor
result.

The three candidates are:

1. **`fts5`** — SQLite FTS5/BM25 over the raw title and description, weighted
   title 5.0 / description 1.0. No model access, so it runs anywhere.
2. **`digest`** — BM25F over an LLM-written digest of each ticket rather than its
   raw prose, with the digest's four fields as separately weighted columns.
3. **`vector`** — embedding similarity over the same digests.

The BM25F field weights are the harness's one free parameter; they are set at the
top of `measure.py` so a re-weighting is a one-line change and is visible in the
diff rather than buried in a function.

Candidates 2 and 3 both read `digests.json`; neither can run without it.

## Rebuilding the digests

Only needed when the corpus changes or the digest prompt changes. It is the one
expensive step: one model call per corpus ticket.

```sh
python evals/similar-tickets/measure.py --build-digests
```

It is **resumable and additive** — it skips any ticket whose digest is already
cached under the current prompt, checkpoints every 25 calls, and reports a
running count of empty replies. Interrupting it loses at most 25 calls.

The cache key covers both the digest input and the prompt text, so editing the
prompt invalidates every entry and a re-run rebuilds the lot.

### Empty replies

The run reports `N calls, M empty`. An empty digest is one the harness could not
parse into any of the four fields, and it degrades that ticket to an empty
document for candidates 2 and 3 — it does not crash the run.

That counter exists because of a bug this harness already had: an early parser
read only the top level of the reply and returned four empty strings in silence,
so a mass failure looked exactly like success. Two parse shapes are now handled —
the model wraps its answer in a single named key, and it answers in YAML rather
than JSON for a minority of tickets. Watch the counter on every rebuild; a sudden
jump means the reply shape moved again, not that the tickets got harder.

## Model access

The digest build needs Bedrock. The eval harness runs under an operator identity,
which is entitled to both the Claude models and the Titan embedding models.

The review-bot instance role is **not**: `infra/terraform/iam_s7.tf` scopes
`bedrock:InvokeModel` to `anthropic.claude-*` and lists no embedding ARN.
Deploying an embedding-based architecture therefore requires an IAM change, which
the ADR records as a precondition. Entitlement for that role must be re-verified
from the instance — a check from an operator identity proves nothing about it.
