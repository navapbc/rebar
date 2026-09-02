# ADR 0110 — Change shape enters the floor as an advisory on file surface, not as a blocking size rule

**Status:** Accepted (epic `wide-wimpy-insect` / `87cb-7121-a3b6-4606`, task `floaty-imperfect-pomeranian` / `0880-0afb-7fe3-48c9`)
**Date:** 2026-09-01

## Context

Track I's plan (I-4) commissioned a measurement before any change-size or decomposition rule
could enter the deterministic floor, and required the enforcement decision to be recorded **either
way**. The field's practice is measurement-first: no enforced change-size tool was found in the
surveyed projects, and Prow's `size/*` labels are non-blocking.

The measurement is `reports/stability/change-shape-backtest.md`, with the two supporting mechanism
reads in `reports/stability/ci-landing-regressions-read.md` and
`reports/stability/environment-failures-read.md`. It covers 2,194 merged Gerrit changes
(2026-07-01 .. 2026-09-02), joined to main commits by revision SHA with a 100% exact match rate,
using the shipped `count_non_test_diff_lines` predicate so the measurement cannot drift from the
gate that ships.

A prior grounding note suggested files-touched and commit fragmentation might be sharper variables
than raw lines. This repository's data confirms the first, refutes the second, and is decisive
about which of the two size variables is real.

## Decision

### 1. File surface is the variable; line count is not

Across 2,194 changes the probability of collecting at least one `Verified-1` before merge rises
monotonically with files touched — 22.7% (1–2 files) → 40.2% → 46.0% → 63.5% (11–20). The effect
**survives a control for line count**, and the reverse control fails:

- holding non-test LOC at 151–400, widening the file surface moves the rate 41.6% → 48.2% → 62.2%;
- holding files at 3–5, adding lines is **non-monotonic**: 34.5% → 45.8% → 41.1%.

Line count's apparent effect is therefore largely lines acting as a proxy for surface. Files is
also the stronger correlate on every independent rework signal measured (patchsets +0.294 vs
+0.236; `caused_by` fan-in +0.190 vs +0.175; plan-review rounds +0.370 vs +0.349).

### 2. The rule is ADVISORY, not blocking

Every rank correlation is ≤ 0.42, and even the widest stratum passes CI first time 38% of the
time. A blocking rule on this evidence would carry roughly a 38% false-positive rate against
changes that were going to land cleanly. The finding is surfaced to the author as coaching; it
does not refuse the change. This matches the non-blocking posture the field already settled on.

### 3. Commit fragmentation is dropped as a variable

1,616 of 1,793 tickets (90.1%) are single-commit, so there is no variance to measure. The apparent
signal is an artefact of Gerrit's model: rework is recorded as **patchsets**, not as extra trunk
commits — over the same window only 11.7% of changes landed on a single patchset. Fragmentation's
one non-trivial correlation (close-gate rounds, +0.258) is almost certainly reverse causation, since
failing the close gate produces another commit.

### 4. No new all-work line threshold is introduced

The shipped bug-fix gate at `BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES = 150` is untouched. Its scope,
corpus and purpose differ from this measurement, and nothing here invalidates it. What this ADR
declines is a *new* line-count rule applied to all work: that number sits at roughly p65 of all
work rather than the p88 it occupies inside the adjudicated bug-fix corpus, and would flag 38.4%
of tickets while measuring the wrong variable.

### 5. The relationship is nonstationary and must be re-tuned, never frozen

Any threshold derived from these bands is a snapshot of this repository at this time. The
measurement is reproducible from the commands recorded in the backtest report; re-run it before
relying on the bands again, and treat a materially different distribution as a reason to re-tune
rather than to reinterpret.

## Consequences

- A change with a wide file surface earns an advisory finding, not a refusal. Authors keep the
  ability to land a broad but correct change without an override.
- Because the rule is advisory, it needs no exception mechanism, no waiver marker, and no
  administrator-locked limit — the three things that make a blocking ratchet expensive to own.
- **The claim is correlational, not causal.** Both shape and rework are downstream of intrinsic
  task difficulty; `caused_by` is a post-hoc attribution biased toward visible changes; and
  plan-review rounds are measured on the plan while lines are measured on the commit produced
  after it. Showing that decomposition *reduces* rework would need an intervention this task did
  not commission.
- Measured rework is a **lower bound**: only `status:merged` changes are counted, so a change that
  failed CI and was abandoned never appears.
- The two supporting reads stand on their own. The CI read found **zero flakes** among 20
  substantiated cases and identified drift as the largest class (9 of 20), which is the same
  mechanism the sibling mirror-inventory sweep found from the other direction. The environment
  read identified one pre-flight check — local install, provider credential, ambient environment —
  covering roughly half of the identified environment incidents, and separated out a class of
  agent-harness defects that were being filed as environmental.
