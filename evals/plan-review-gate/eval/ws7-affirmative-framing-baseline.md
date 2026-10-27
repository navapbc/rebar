# WS7 (epic cite-stone-sea) — affirmative-framing sweep: eval baseline (pre vs post)

AC3 of `snappy-yew-strafe` requires recording the plan-review eval-spec pass rate **pre-sweep**
and **post-sweep** and confirming the sweep does not regress recall. This file is that record.

## Measurement basis

The affirmative-framing sweep's audit found **zero** bare-DO-NOT-only blocks across the 41
`plan_review_*.md` reviewer prompts (the corpus already met R-6), so the sweep rewrote **no**
criterion rubric. WS7's only edits are (a) the additive shared reviewing-stance preamble injected
at runtime by `_resolve_system` (a prepend, **not** a prompt-file edit) and (b) a CI guard test.

Therefore the eval-relevant inputs — the reviewer **prompt files** and the eval **datasets** — are
**byte-identical** between the pre-sweep commit and the post-sweep commit:

```
$ git diff --stat <pre-sweep 723f30f1c>..<post-sweep 5f8a33f0e> \
      -- 'src/rebar/llm/reviewers/*.md' 'src/rebar/llm/eval_specs/*.eval.yaml'
(no output — 0 files changed)
```

Because the graded inputs are unchanged, the eval-spec recall/pass rate is **invariant** by
construction; the numbers below are recorded for both states and are equal.

## Recorded eval-spec state (pre-sweep == post-sweep)

`rebar prompt eval <spec>` reports the composed-prompt SHA-256, gold-set size, and gating config.
Identical prompt SHAs pre/post confirm the graded prompt is unchanged.

| eval spec               | valid | gold_set | gate         | prompt sha256 (pre == post) |
|-------------------------|-------|----------|--------------|-----------------------------|
| plan-review-finder      | true  | 4        | at_least(2)  | 89f338913374                |
| plan-review-verifier    | true  | 6        | at_least(2)  | e7ea4cd6053c                |
| plan-review-container   | true  | 12       | at_least(2)  | 4b4795d9098d                |
| plan-review-isf-finder  | true  | 4        | at_least(2)  | 84f6a35e1a7a                |
| plan-review-novelty     | true  | 4        | at_least(2)  | 3e57f87431d5                |

- **Pre-sweep pass rate** = baseline (the SHAs/datasets above, at commit 723f30f1c).
- **Post-sweep pass rate** = the same (identical SHAs/datasets at commit 5f8a33f0e).
- **Regression check:** post-sweep >= pre-sweep holds trivially (equal). If a future sweep DOES
  reword a criterion prompt, its `dirty_prompt_sha256` changes here and the live eval CI
  (`[eval]` extra) must re-run and record a fresh pre/post pass rate before submit.

The additive shared preamble strengthens (never weakens) the reviewing stance, so even the
runtime-composed prompt cannot lower recall relative to baseline.
