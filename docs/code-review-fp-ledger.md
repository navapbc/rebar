# Code-review false-positive ledger

When the code-review gate emits a finding that turns out to be a **confirmed false positive**
(wrong, invalid, or out-of-scope), record it as a rebar ticket so the incident becomes a
**standing regression** the reviewer is held to. `compile_fp_ledger`
(`src/rebar/llm/code_review/fp_ledger.py`) turns each ledger ticket into a NO-FIRE eval case —
a `expect: pass` entry in the `code-review-*.eval.yaml` dataset shape — so a re-introduced false
positive fails the eval.

This is **advisory tooling only**: it never runs inside the gate, never touches a verdict, and
never adds a blocking source. It is **manual** (no scheduling) — you call `compile_fp_ledger`
and feed the drafted cases into an eval dataset.

## The `fp:code-review` tag convention

An FP-ledger entry is a **`bug`-typed ticket tagged `fp:code-review`** recording one confirmed
false-positive / invalid code-review finding. Only **OPEN** tickets are compiled; a ticket is
skipped once it carries the `compiled` tag (see idempotency below).

### Required body fields

The ticket description must carry:

- **The finding** — the criterion it fired under plus the finding text (so a reader knows what
  the reviewer claimed).
- **The diff/context that triggered it** — the change the reviewer was looking at, in a **fenced
  code block** (the first fenced block in the body is used verbatim as the eval case's `diff`).
- **A `root-cause:` line** — one value from the closed enum below (case-insensitive, e.g.
  `root-cause: false-evidence`).

A ticket missing the root-cause line or the fenced diff block is **skipped** (logged, not fatal).

Example body:

```markdown
## Finding
criterion: code-review-tests
finding: flagged the test as tautological, but it asserts a real observable postcondition.

## Root cause
root-cause: false-evidence

## Diff / context
```diff
--- a/tests/test_claim.py
+++ b/tests/test_claim.py
@@ -1,3 +1,7 @@
+def test_claim_conflict_raises(store):
+    store.claim("t1", "a")
+    with pytest.raises(ConcurrencyError):
+        store.claim("t1", "b")
```

## Acceptance Criteria
- [ ] the tests overlay no longer fires on this diff
```

## Root-cause enum

| Value | Meaning |
|-------|---------|
| `false-evidence` | The finding cited evidence (a line/behaviour) that the diff does not actually contain. |
| `rubric-overapplication` | A valid rubric rule applied where it does not belong (a false match). |
| `hallucinated-gap` | The reviewer invented a missing requirement/test/case that is not actually absent. |
| `scope-mismatch` | The finding is real but out of scope for the change under review. |
| `stale-baseline` | The finding is about code the diff did not touch (reviewed against a stale baseline). |

## How `compile_fp_ledger` drafts cases

`compile_fp_ledger(repo_root=None) -> list[dict]`:

1. Reads OPEN `fp:code-review` tickets via the rebar library (`list_tickets`).
2. For each ticket **not** already tagged `compiled`, drafts a no-fire eval case in the dataset
   shape `{id, corpus: "fp-ledger", expect: "pass", mode: <root-cause>, diff: <fenced block>}`
   (`id` is a slug from the ticket alias/id).
3. **Idempotent** — stamps the `compiled` tag on each drafted ticket, so a re-run skips it.
4. **Error-isolated** — a store-read failure returns `[]` (never raises); one malformed/unreadable
   ticket is logged-and-skipped without aborting the batch.

**Rule-of-Three.** Don't over-fit the reviewer to a single incident. Treat the ledger as a
signal: once **three** entries share a `root-cause`, that recurring failure mode is worth a
prompt/rubric change (not just per-diff no-fire cases). The advisory `approach_viability_note`
telemetry on the code-review verdict (`coverage.approach_viability_note`) surfaces the same
Rule-of-Three intuition at review time.
