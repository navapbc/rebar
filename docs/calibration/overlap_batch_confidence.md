# Batched overlap-judge repair (ticket d147-c219-baf9-4e9e)

Overlap detection had never surfaced a finding: zero of 2,093 recorded plan reviews carried a
non-empty `overlap` block. The changed artifacts are a **prompt** and a **structured-output
contract**, so — as with the T5c recalibration — canned-runner unit tests cannot pin the
behaviour. These are **live** Bedrock runs against `bedrock:us.anthropic.claude-sonnet-4-6`, the
`standard` class the judge binds per call.

Reproduce:
`REBAR_LLM_BEDROCK_REGION=us-east-1 python scripts/overlap_judge_calibrate.py`
(exit 0 on PASS). `overlap_conf_threshold = 0.7`.

## What was actually wrong

Two defects stacked, and the second hid behind the first.

1. **The batch entry contract made every judgement field optional.** `OverlapVerdictEntry`
   subclassed the single-pair model, inheriting its all-defaulted fields, and added
   `candidate_id` with `default=""`. The emitted JSON Schema therefore had **no `required`
   array at all**. The single-pair model's leniency is a deliberate safe-default for ONE
   object; repeated across a LIST it stopped being safe. Two consequences, both measured:
   `confidence` was simply omitted and defaulted to **0.0** — below the 0.7 threshold, so
   `aggregate` could never surface anything — and the model's multi-entry tool arguments
   **degenerated outright**, whole entries collapsing into the first string field.
2. **`output_token_limit=1024` was flat.** Sized for a single verdict, it truncated any real
   multi-candidate batch. Truncation is not a partial answer: the stop-reason guard rejects the
   turn, so the **whole batch** abstained.

The one-candidate case worked fine throughout — which is exactly why the failure survived. A
probe that judges a single pair at a time reproduces nothing.

## Before

| Probe | Result |
|---|---|
| 1-candidate batch (near-duplicate), flat 1024 | `duplicates`, confidence **0.97 / 0.95** — **passes**, and masks the defect |
| 6-candidate batch, flat 1024 (production shape) | **all 6 abstain**, `confidence=0.0`; call raises `Exceeded maximum output retries (0)` |
| 6-candidate batch, limit raised to 2048 / 4096 | returns, but **malformed**: entry count drifts (8, 2, 4 across runs) and `candidate_id` swallows its siblings, e.g. `"near_duplicate','relation':'duplicates','shared_artifact':'judge_batch output_token_limit','confidence':0.93,'abstain':false},{"` |
| 6-candidate batch, **no** output clamp at all, 3 trials | still malformed in **3/3** — proving the token cap was not the only fault |

The malformed shape is doubly fatal: `judge_batch` drops a second entry for an id it has
already read, so when the corrupt entry came first, the *correct* verdict that followed it was
discarded.

## After

Contract fields `candidate_id` / `relation` / `confidence` / `abstain` are **required**
(`shared_artifact` stays nullable — a null there is a meaningful answer, not an omission), the
prompt's batch section restates rule 3 per entry, and the output budget is
`_OUTPUT_TOKENS_BASE + _OUTPUT_TOKENS_PER_VERDICT * n` = **1408** for a full 6-candidate batch.

| Candidate | Ordering 1 | Ordering 2 | Surfaced |
|---|---|---|---|
| `near_duplicate` | `duplicates` / **0.95** | `duplicates` / **0.95** | **yes** |
| `unrelated_0…4` (5 distractors) | `unrelated` / 0.98 | `unrelated` / 0.97 | no |

Isolated-pair cases: near-duplicate `duplicates` 0.95/0.95 → surfaced; unrelated
`unrelated` 0.95/0.95, `shared_artifact: null` → not surfaced. Verdict entries measured at
117–151 bytes of JSON (~230 bytes at the widest observed `shared_artifact`, ~65 tokens), which
is the measurement behind `_OUTPUT_TOKENS_PER_VERDICT = 192` (~3x headroom).

## Reading

- **Recall restored, from zero.** The near-duplicate now clears the threshold in BOTH orderings
  and `aggregate` emits a finding. This is the first time the mechanism has been shown to work
  end-to-end on a live call.
- **Precision intact.** All five distractors, and the isolated unrelated pair, stay unsurfaced
  with a null `shared_artifact`. Making fields required did not make the judge generous — the
  confidences it now reports on `unrelated` verdicts (0.97–0.98) are confident *negatives*.
- **The storm guards are untouched.** `structured_retry_limit=0` and
  `transport_attempt_limit=1` are unchanged and unit-asserted; the budget is still bounded by
  `_CANDIDATES_PER_CALL`. The 48k retry-storm class that motivated the flat cap stays dead —
  what changed is only that one correctly-sized attempt can finish its answer.
- **A required field is load-bearing, not cosmetic.** The headline lesson: an all-optional
  structured-output schema does not merely permit omissions, it measurably degrades the model's
  generation for repeated entries. Per ADR 0086 the overlap step is advisory, so its failure
  mode was silent — a contract that cannot fail loudly needs a live probe to stay honest.

Per the ticket's step 4, the surfaced-findings rate should be re-measured over the next N plan
reviews now that the mechanism is provably working.
