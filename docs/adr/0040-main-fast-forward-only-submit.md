# ADR 0040 — `main` submit type → Fast Forward Only (make it impossible to land a stale/untested tree)

**Status:** Accepted
**Date:** 2026-07-10
**Amends:** ADR 0020 (two-vote CI gate), ADR 0025 (feature-branch merge-carry — corrects its submit-type claim)

## Context

The Gerrit `Verified` vote certifies a change against **its own** (possibly stale) base:
CI runs on the patchset as uploaded (`refs/changes/NN/NNNN/P`), never rebased onto the
current `main`. With the ADR-0025 submit type **`merge if necessary`**, submit then lands
the change **merged onto whatever `main` has become**, and Gerrit does **not** re-run CI on
that merged tree. So two changes that are each green on their own base can produce a broken
`main` when merged — a semantic conflict a clean 3-way content-merge resolves but that breaks
the build/tests. This actually happened (the `IDEA`-status test: one change made `IDEA` a
mapped Jira status while a separately-green change asserted `IDEA` was *unmapped*; the merged
tree was red, both gates green). See the "stale-base merge" analysis and the requirements:

- **R1** every change passes CI + LLM review before landing;
- **R2** features reviewed as small stacked changes;
- **R3** a stack lands atomically as one coherent feature;
- **R4** many parallel change-sets land gracefully;
- **R5** it is impossible to land a change that breaks CI on `main`;
- **R6** emulate a proven, actively-maintained OSS workflow rather than invent one.

A full speculative merge queue (Zuul) is the throughput-preserving answer but is
cost-disproportionate at this scale: it cannot use GitHub Actions as its runner (it owns
execution via Ansible/Nodepool), so adopting it means re-platforming CI and standing up a
ZooKeeper + SQL + scheduler + executor + Nodepool control plane — ~3-4× the monthly infra
bill plus a permanent ops treadmill, for a single repo at ~26 merges/day. Rejected on cost.

## Decision

Set the `rebar` project submit type to **Fast Forward Only** (`[submit] action = fast forward only`).

A change (ordinary change, relation chain, or feature-branch `--no-ff` merge commit) is
**submittable only if it is a descendant of the current `main` tip** — i.e. built directly on
top of it. Gerrit never auto-merges or auto-rebases onto a newer `main`. When `main` advances
under an in-review change, the change becomes **non-submittable** until it is rebased
(ordinary change / relation chain) or re-merged (feature-branch change) onto the new tip.

**Why this delivers R5 (the load-bearing chain):**

1. FFO refuses to submit any change whose base is not the current `main` tip.
2. Rebasing/re-merging onto the new tip mints a **new patch set with a new tree**, of change
   kind `TRIVIAL_REBASE` (or larger) — **not** `NO_CODE_CHANGE`.
3. The `Verified` label's `copyCondition = changekind:NO_CODE_CHANGE` (ADR 0020) therefore
   **does not copy** the prior vote → `Verified` is dropped → **CI re-runs against the new
   tree**.
4. The patch set that finally carries `Verified +1` is the *same tree* that FFO fast-forwards
   onto `main`. **No stale tree and no untested tree can land.**

`copyCondition` **must** exclude `changekind:TRIVIAL_REBASE` for this to hold (it does —
`NO_CODE_CHANGE` only). Adding `TRIVIAL_REBASE` to reduce CI churn would silently break R5.

**Requirement coverage:** R1 unchanged (two-vote gate). R2 via Gerrit relation chains
(each commit reviewed independently). R3 via `change.submitWholeTopic = true` (already set) —
a topic's linear changes submit atomically. R5 as above. This is the Gerrit-native "commit
queue" idiom (the Android/Chromium/Go lineage), not an invented mechanism (R6).

## Correction to ADR 0025

ADR 0025 §Context(2) asserted that "a fast-forward-only … submit strategy would defeat the
atomic `--no-ff` merge-back." **That is not correct for Fast Forward Only** (it conflated FFO
with *cherry-pick*, which rewrites commits). Gerrit's config reference is explicit: *"Gerrit
does not create merge commits on submitting a change, but merge commits that are created on
the client, prior to uploading to Gerrit for review, may still be submitted."* A `--no-ff`
merge commit whose first parent is the current `main` tip **fast-forwards** and is accepted by
FFO. The feature-branch flow (ADR 0025) is therefore **unchanged**, except that a re-merge onto
a moved `main` becomes *mandatory* to submit — which ADR 0025's `MERGE_FIRST_PARENT_UPDATE`
re-merge step already performs. FFO simply makes that re-merge non-optional, which is exactly R5.

## Consequences / accepted costs

- **R4 is the compromise.** FFO has no queue and no speculation: after one change submits, every
  other open change on `main` becomes non-submittable until rebased. Under contention (measured:
  median inter-landing gap ≈ CI duration; ~46 % of landings overlap a prior CI run) this is a
  manual **rebase-treadmill** — correct, but not graceful. Accepted for now; if the treadmill
  becomes real pain, revisit a queue (a lightweight Gerrit commit-queue bot before Zuul).
- **`submitWholeTopic` + parallel siblings.** FFO can atomically submit a *linear* topic (a
  stack), but a single topic containing **parallel sibling** changes to `main` cannot be
  linearized and its atomic submit fails (the Wikimedia FFO limitation). rebar lands independent
  changes (distinct topics) and linear relation chains, so this is not a practical constraint;
  do not put mutually-parallel changes to `main` in one topic.
- **Project-global.** Submit type cannot be scoped per-branch, so FFO also governs
  `refs/heads/feature/*`. This is consistent with the model (stories are fast-forwardable
  stacked changes on the feature branch; the branch lands as one FF merge change).
- **Back-out** is one line: `action = merge if necessary` (the ADR-0025 prior state).

## Deployment

`project.config` → `refs/meta/config` is **detect-only** under autodeploy (ADR 0026): landing
this change does **not** flip production. A **manual operator apply** is required — run
`infra/gerrit/setup-project.sh` with the Gerrit admin SSH key (`DRY_RUN=1` first to diff live vs
desired). Because the flip makes every in-flight change across all sessions non-submittable
until rebased onto tip, apply it at a quiet moment and announce it. No data is lost — in-review
changes remain; they simply rebase (and re-CI) before they can submit.
