# Adjudication rubric A — plan-review finding TP/FP (primary rater)

You are adjudicating a single plan-review finding. A plan reviewer (an LLM gate) raised
this finding against a ticket's plan under a specific criterion. Your job is to decide
whether the finding is a **true positive (TP)** or a **false positive (FP)** — i.e.
whether the gate was *right* about this finding.

## What TP / FP mean (read carefully — it depends on `source`)

A finding is either `source: surfaced` (the gate showed it to the agent as a real
concern) or `source: dropped` (the gate computed it but SUPPRESSED it — an
advisory-overflow or a Pass-3 floor drop — so the agent never saw it).

- **surfaced finding**
  - `TP` = the criterion **genuinely applies** here: the finding names a real weakness
    in the plan (a missing/ vague acceptance criterion, an unbacked "X already exists"
    claim, a placeholder, a coherence contradiction, an unjustified destructive step,
    etc.). Surfacing it was correct.
  - `FP` = the finding is **spurious**: the plan does not actually have the problem the
    finding asserts (the reviewer misread the plan, the concern is already addressed,
    or the "defect" is not a defect). Surfacing it was noise.

- **dropped finding** (the lens INVERTS — the gate chose NOT to surface it, so you are
  judging the *drop decision*, not the finding text)
  - `TP` = suppressing it was **correct**: there is no real defect here (the finding is
    spurious / already handled by the plan), so hiding it from the agent was the right
    call (a good drop).
  - `FP` = suppressing it was **wrong**: this was a **real defect** the gate wrongly
    hid — the agent should have seen it. (A bad drop / an escaped defect.)

  > ⚠️ **The #1 mistake on dropped findings — do NOT apply the surfaced lens.** For a
  > *surfaced* finding, "the finding is spurious" → FP. For a *dropped* finding it is the
  > **opposite**: a spurious finding that was dropped is a **TP** (the gate was right to
  > hide the noise). Worked example: a dropped finding claims "the plan never names the
  > new flag," but you read the plan and it *does* name the flag → the finding is
  > spurious → **for a DROPPED finding that is `TP`** (correct suppression), NOT `FP`.
  > Conversely, a dropped finding that cites a *real* code fact the plan got wrong is a
  > genuine defect the gate buried → **`FP`** (escaped defect).

- `ambiguous` = you genuinely cannot tell from the finding text, criterion, and plan
  provided (insufficient context, or a defensible call either way). Use sparingly.

## How to judge

1. Read the `criterion` and its meaning, the finding's `finding` text, its
   `suggested_fix` and `location`, and the ticket plan (provided below).
2. Judge the finding **on its criterion** — does the plan, as written, actually violate
   that criterion at that location? Do not import your own criteria.
3. For a surfaced finding, ask: *is this a real, actionable weakness in the plan?* For a
   dropped finding, ask: *would a careful reviewer have wanted the agent to see this?*
4. Prefer `TP`/`FP` over `ambiguous`; only abstain when the text truly underdetermines
   the call.

## Output — EXACTLY one JSON object, nothing else

```json
{"finding_id": "<echo the finding_id>", "tp_fp": "TP|FP|ambiguous", "rationale": "<one or two sentences: WHY>"}
```
