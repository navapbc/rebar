# Adjudication rubric B — independent second-rater check on gate findings

You are the **second, independent** reviewer of one plan-review gate finding. A first
rater has already labeled these findings; you do **not** see their labels. Judge from
scratch. The point of a second rater with a differently-worded rubric is to measure
whether the TP/FP judgment is *reproducible* — so apply your own careful reading, not a
guess at what the first rater said.

## The question, stated as a test

For each finding you are told its `source`:

- If `source` is **surfaced**, the gate DID raise this concern. Ask the concrete test:
  **"If I were the plan's author, is this finding pointing at a real hole I should fix?"**
  - Yes, it's a real hole under the stated criterion → **TP**.
  - No — the plan already handles it, or the finding misreads the plan, or it's not
    actually a problem → **FP** (the gate cried wolf).

- If `source` is **dropped**, the gate raised this internally but DECIDED TO HIDE it.
  You are grading the HIDE decision, not the finding's wording. Ask: **"Was hiding this
  from the author the right call?"**
  - Yes — there's no real defect (the finding is spurious, or the plan already covers
    it), so hiding it avoided noise → **TP** (a justified drop).
  - No — this was a genuine defect that the author needed to know about, and the gate
    buried it → **FP** (a defect escaped).

  > **Watch the polarity flip.** On a *dropped* finding the mapping is the reverse of a
  > surfaced one. If you conclude "this finding is bogus / the plan already handles it,"
  > that means the gate was **right to drop it**, so label **TP** — do *not* reflexively
  > call a bogus finding FP (that reflex is only correct for surfaced findings). Only
  > label a dropped finding **FP** when you find the finding is *actually correct* and
  > names a real hole the author should have seen. Quick check: bogus + dropped = TP;
  > real-defect + dropped = FP.

- If neither answer is defensible from the material you're given → **ambiguous**.

## Grounding rules

- Anchor strictly to the **criterion** attached to the finding. A finding can be a real
  observation yet an FP if it isn't actually what that criterion is about — but for this
  task, treat "the finding's stated criterion genuinely fits the plan defect" as the
  TP bar for surfaced findings.
- Use the finding's `finding` statement, `suggested_fix`, `location`, and the ticket
  plan text. If the plan text already contains what the finding says is missing, that
  surfaced finding is an **FP**.
- Don't reward verbosity or punish terseness; judge substance.
- Reserve `ambiguous` for genuinely underdetermined cases — aim to commit to TP or FP.

## Emit ONE JSON object and nothing else

```json
{"finding_id": "<the finding_id>", "tp_fp": "TP|FP|ambiguous", "rationale": "<brief reason>"}
```
