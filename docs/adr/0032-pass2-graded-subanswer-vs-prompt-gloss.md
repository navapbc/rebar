# ADR 0032 — When a DSO gap becomes a graded Pass-2 sub-answer vs. a prompt gloss

**Status:** Accepted (epic cite-stone-sea — DSO plan-review gap adoption / WS1 — neat-spa-ulcer)
**Date:** 2026-07-06

## Context

The DSO plan-review gap report names three false-positive/defect classes rebar's Pass-2
verifier did not encode (gap-report §4): findings that rest on an unverified factual claim
used to *exclude* work (G-4 / G-7), findings pitched at the wrong **altitude** (FP-1 — they
demand a detail the artifact legitimately defers to a child/implementation), and findings
that merely **restate** what the plan already says (FP-2 — a null delta).

rebar's four-pass gate keeps Pass-2 (verify) and Pass-3 (decide) **generic across findings**:
the verifier answers a fixed, typed set of binary sub-questions (`GRADED_BINARY`), and
`decide.validity()` is the arithmetic mean of the answerable ones. Criterion-specific logic
lives in the reviewer **rubrics**, never in the shared passes. So each adopted class must be
placed on the right seam: a **new graded sub-answer** (participates in the validity mean for
every finding) or a **prompt gloss** on an existing sub-answer (sharpens how the verifier
answers a question that already exists).

Two placement forces:

- A **new graded sub-answer** changes the Pass-3 arithmetic contract and the REVIEW_RESULT
  sidecar shape. It is the right home for a class that is a *distinct* validity axis not
  captured by any existing question — but it must not silently move validity for findings
  where the class does not apply, nor break comparability with sidecars written before it.
- A **prompt gloss** is the right home when the class is a sharper *reading* of an existing
  sub-answer — no contract change, no arithmetic change, so old and new sidecars stay
  directly comparable.

## Decision

**Two new graded sub-answers; one prompt gloss.**

1. `committed_work_relies_on_unbacked_claim` — **new `GRADED_BINARY` sub-answer.** Materiality
   is keyed on a **committed element** (an AC / task / edit / scope exclusion) resting on a
   factual claim with no verification-or-fallback — an objective test, not a subjective
   "load-bearing?" judgement. It is a genuinely new validity axis that unifies the
   confident-assertion (G-7a) and false-exclusion (G-4) finding shapes, and it is the axis the
   WS2 hedge finder and E4 exclusion scan route their findings through for Pass-2 grading.

2. `respects_artifact_altitude` — **new `GRADED_BINARY` sub-answer** (FP-1). It is a **graded
   validity input, not a veto**: an altitude error (`no`) *lowers* validity like any other
   sub-answer and lets the arithmetic drop the false positive, rather than hard-vetoing (which
   would concentrate a fragile all-or-nothing judgement in one question).

3. **Restatement / null-delta (FP-2) — a prompt gloss on `evidence_entails_finding`,** not a
   new sub-answer. A finding that merely restates an existing consideration, done-definition,
   or already-declared dependency is precisely a case where the cited evidence does **not
   entail a defect** — the existing load-bearing question already owns that judgement. Adding a
   fourth axis for it would double-count skepticism (gap-report R-3). The gloss is carried
   verbatim ("already states") in both verifier prompt variants.

**Backward-compatibility policy (na-default).** Both new sub-answers default to **`na`** in the
Binary model (not `insufficient`), via a data-driven default set over the *same* uniform
`GRADED_BINARY` loop — no per-criterion branching in the pass. `decide.validity()` already
excludes any non-`yes|no|insufficient` value from the mean, so:

- old sidecars (which lack the keys entirely) read identically — the absent key is excluded,
  exactly as `na` is; and
- a new verifier that does not engage a class abstains (`na`, excluded) instead of dragging
  validity toward 0.5 — the two keys move validity **only** when the verifier affirmatively
  answers `yes`/`no`/`insufficient`.

## Consequences

- Pass-3 arithmetic and the coach registry stay generic; the two keys ride the existing
  uniform loop in `review_kernel/verify.py` and are iterated by `review_kernel/decide.py`.
- The REVIEW_RESULT sidecar gains two optional binary keys; offline replay of pre-change
  findings yields identical validity (guarded by a unit test in
  `tests/unit/test_review_kernel_rules.py`).
- Adopting a future gap class now has a documented rule: new graded axis only when it is a
  distinct, always-applicable-or-`na` validity dimension; otherwise a gloss on the existing
  sub-answer that already owns the judgement.
