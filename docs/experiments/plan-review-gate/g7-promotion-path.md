# G7 (leaf-parent-containment) promotion path

G7 is a NEW AGENT-tier, **advisory** (block_threshold `0.95`) plan-review criterion (ticket
d4cf). It fires ONLY on a **leaf** ticket **with a parent** and checks the leaf's declared
scope is a SUBSET of its parent's plan (parent wins on conflict — see
`src/rebar/llm/reviewers/plan_review_G7.md`). This document defines how we will observe G7 in
the field and the trigger for promoting it from advisory to blocking. It is a
committed, CI-verifiable artifact (AC4); it does NOT itself flip any posture.

## 1. Fire-rate observation query

G7 emits findings into the standard `plan_review_result_v1` REVIEW_RESULT sidecars. We measure
the **fire-rate** over the observation window with a **jq** query that reads only sidecars whose
`.schema == "plan_review_result_v1"`.

G7 fire-rate = (count of sidecars whose `.findings[]` includes "G7" in `.criteria` **and** whose
`.decision != "dropped"`) / (count of G7-**eligible** reviews — those that routed G7, i.e. a
leaf-with-parent review). Each surviving finding's `.validity` is read alongside so we can
separate high-validity fires from low-validity noise.

```sh
# Numerator: sidecars with a surviving (non-dropped) G7 finding, plus each finding's validity.
jq -s '
  map(select(.schema == "plan_review_result_v1"))
  | map(.findings[]? | select((.criteria // [] | index("G7")) and (.decision // "") != "dropped"))
  | { g7_fires: length,
      validities: (map(.validity) | group_by(.) | map({ (.[0]|tostring): length }) | add) }
' path/to/*.plan_review_result_v1.json

# Denominator: G7-eligible reviews (a review that routed G7 at all — leaf-with-parent).
jq -s '
  map(select(.schema == "plan_review_result_v1"))
  | map(select(.routing // [] | index("G7")))
  | length
' path/to/*.plan_review_result_v1.json
```

Fire-rate = numerator `g7_fires` / denominator eligible-count. Track the `validities` histogram
to estimate the **false-fire rate** (low-validity fires the plan author would reasonably reject).

## 2. Promotion trigger

Promote G7 from **advisory** to **blocking** by lowering its `block_threshold` from `0.95` into
the standard blocking band of **`0.6`–`0.7`** (the band the other blocking criteria use — e.g.
G5/F1 at `0.6`, T1/T4 at `0.7`) **once** the observation window shows an acceptable false-fire
rate at the candidate threshold. Concretely: pick `0.6` if the validity histogram shows fires are
overwhelmingly high-validity, `0.7` if a slightly tighter bar is needed to suppress marginal
noise; flip `default_posture` to `"blocking"` in the same routing edit.

## 3. The actual decision is DEFERRED

The **actual** promotion decision depends on **post-deployment field data** — G7 must run in the
live gate for a real observation window before we can trust the fire-rate / false-fire numbers.
That decision is therefore **out of this ticket's (d4cf) close path** and is DEFERRED to the
epic's deferred-measurement follow-up ticket **72b6-b0d2-2f93-456d**. This ticket ships G7
advisory-only with the query and the trigger documented; the follow-up ticket owns collecting the
window and executing (or declining) the promotion.
