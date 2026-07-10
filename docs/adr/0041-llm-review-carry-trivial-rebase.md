# ADR 0041 — Carry the LLM-Review vote across `TRIVIAL_REBASE` (preserve review on a non-conflicting update from `main`)

**Status:** Accepted
**Date:** 2026-07-10
**Amends:** ADR 0025 (reverses its deliberate `TRIVIAL_REBASE` exclusion for the LLM-Review label). Complements ADR 0040 (Fast Forward Only submit type).

## Context

ADR 0040 set `main` to the **Fast Forward Only** submit type: a change is submittable only
when it sits on the current `main` tip, so when `main` advances an in-review change must be
**rebased onto the new tip** to submit. That rebase mints a new patch set. Its change kind is
`TRIVIAL_REBASE` when the rebase was **conflict-free and the diff (including context lines) is
byte-identical** to the prior patch set — i.e. a "non-conflicting update from `main` that does
not alter the change under review."

Under the prior config, **both** gate votes dropped on that rebase: `Verified` (correct — CI
must re-test the new integrated tree) **and** `LLM-Review`. ADR 0025 had **deliberately
excluded** `TRIVIAL_REBASE` from the LLM-Review `copyCondition`, reasoning it would copy the
vote onto "a tree the LLM never reviewed." Consequence: every FFO rebase-to-tip forced a *full
re-review* by the LLM bot in addition to a CI re-run — the dominant, and largely wasted, cost
of the FFO rebase-treadmill, since the reviewed **diff** did not change.

## Decision

Add `changekind:TRIVIAL_REBASE` to the **LLM-Review** label's `copyCondition`:

```
copyCondition = changekind:NO_CODE_CHANGE OR changekind:MERGE_FIRST_PARENT_UPDATE OR changekind:TRIVIAL_REBASE
```

The LLM-Review vote now **carries** across a conflict-free, diff-identical rebase onto a newer
`main`. It still **drops** on any real edit or a conflicting rebase (both classified `REWORK`),
which forces a fresh review. **`Verified`'s `copyCondition` is UNCHANGED** (`NO_CODE_CHANGE`
only) — CI still re-runs on every rebase.

## Rationale — the diff-vs-tree asymmetry (corrects ADR 0025's reasoning)

ADR 0025's concern ("a tree the LLM never reviewed") conflated two different things the two
votes certify:

- **LLM-Review certifies the change's DIFF.** `TRIVIAL_REBASE` is *defined* by diff-identity
  (Gerrit: same commit message **and** same diff including context lines, with no conflict
  resolution). So the exact delta the LLM approved is provably unchanged — the review still
  holds, and re-review is pure friction.
- **`Verified` certifies the INTEGRATED TREE.** A rebase onto new `main` changes that tree even
  when the change's own diff is identical, so `Verified` must (and does) drop and re-run.

Keeping `Verified` strict while letting the review vote carry across `TRIVIAL_REBASE` is exactly
the **GerriScary / CVE-2025-1568-hardened** posture: the vulnerability was an over-permissive
copyCondition on the *auto-merge/CI* label letting an injected patch set ride an approval into a
submit. That vector stays closed here — `Verified` is untouched. Carrying the *review* label
across `TRIVIAL_REBASE` mirrors Gerrit's own default for its built-in **Code-Review** label, so
this is the standard, intended configuration for a review-class label.

**Accepted residual risk:** the LLM reasoned about the change against its *old* base; a trivial
rebase pulls in new `main` code it did not see, and it will not re-flag a *semantic* regression
(non-conflicting, not caught by the type checker) introduced by that new code. But that is
**integration correctness**, which is `Verified`'s job — and `Verified` re-runs on every rebase.
This is the same residual Gerrit already accepts for human reviewers under its Code-Review
default. Accepted.

## Consequences

- **The FFO rebase-treadmill (ADR 0040 R4 cost) is materially cheaper:** a rebase-to-tip now
  re-runs only CI, not the (billable, slower) LLM review. This is the low-risk half of the
  treadmill mitigation; automating the rebase *action* itself (an auto-rebase bot / commit
  queue) is separate, unbuilt, and deferred — it is a new service with its own design decisions.
- **Back-out** is one line: remove `OR changekind:TRIVIAL_REBASE` from the LLM-Review
  `copyCondition` (restores ADR 0025's exclusion).
- **Deploy:** `refs/meta/config` is detect-only under autodeploy (ADR 0026); the live apply is a
  manual operator push of `project.config` to `refs/meta/config` with an admin credential (as
  for ADR 0040).
