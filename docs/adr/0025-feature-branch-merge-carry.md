# ADR 0025 — Feature-branch merge-carry: `MERGE_FIRST_PARENT_UPDATE` copyCondition + submit-type pin + `feature-branch-drivers` ACLs

> **Note (2026-07-13):** `main`'s submit type is now **Rebase-If-Necessary** per [ADR 0047](0047-retire-autolander-rebase-if-necessary.md); this ADR's references to a fast-forward-only submit type describe the prior model. The feature-branch machinery it decides (the `MERGE_FIRST_PARENT_UPDATE` copyCondition and `feature-branch-drivers` ACLs) remains in effect — but how Rebase-If-Necessary interacts with the merge-back re-merge/re-CI cost is not re-derived here and needs human review.

**Status:** Accepted (epic 88ab / story S1 — bored-tag-sale)
**Date:** 2026-07-02
**Amends:** ADR 0013 (LLM-Review label) and ADR 0020 (two-vote CI gate)

## Context

Epic 88ab adopts the OpenDev-documented **server-side feature branch + reviewed merge
change** pattern for multi-story features (validation session log apt-otter-urn, 2026-07-01
proved the chain/topic flow cannot satisfy R1–R5). Stories are reviewed INTO
`refs/heads/feature/<name>` as they pass both gates; a single `--no-ff` merge change of the
branch into `main` is then gated identically and submitted atomically. Three Gerrit-side
changes are needed to make this safe:

1. When `main` advances under an open merge change, re-merging produces a
   `MERGE_FIRST_PARENT_UPDATE` patchset (first parent moves, feature tip unchanged). Under
   ADR 0013's strict `copyCondition = changekind:NO_CODE_CHANGE`, the LLM-Review vote drops
   on every such re-merge and forces a needless re-review — the very "invalidation fragility"
   (R4) the pattern exists to avoid.
2. The project's submit type is inherited (not pinned). A fast-forward-only or cherry-pick
   submit strategy would defeat the atomic `--no-ff` merge-back.
3. Merge commits and `feature/*` branch lifecycle need a bounded, auditable write path.

## Decision

### 1. LLM-Review carries across `MERGE_FIRST_PARENT_UPDATE`; Verified does not

`[label "LLM-Review"] copyCondition = changekind:NO_CODE_CHANGE OR changekind:MERGE_FIRST_PARENT_UPDATE`

A `MERGE_FIRST_PARENT_UPDATE` re-merge integrates the **same reviewed feature tip** onto a
moved `main`; the auto-merge delta the bot reviews (S2) is unchanged, so carrying the LLM
vote certifies exactly what was reviewed. Changing the feature tip is **not** a
`MERGE_FIRST_PARENT_UPDATE` (it is REWORK) → the vote drops and a fresh review is forced.
`TRIVIAL_REBASE` is still deliberately excluded (ADR 0013 rationale unchanged).

**Verified's copyCondition is left UNCHANGED (`changekind:NO_CODE_CHANGE`) — the deliberate
divergence.** A re-merge produces a **new merge tree** (main's new commits are now in it)
even though the first-parent-only change carried the LLM-reviewable delta. CI must therefore
**re-run** — a stale CI vote must never carry onto a tree CI never built (GerriScary /
CVE-2025-1568 posture, ADR 0020). So on a `MERGE_FIRST_PARENT_UPDATE`: **LLM-Review carries,
Verified re-runs.** This asymmetry is intentional and is the core of the cost model (S6):
re-reviews avoided (LLM) vs CI re-runs incurred.

### 2. Submit type pinned to `merge if necessary` + content merge

```
[submit]
    action = merge if necessary
    mergeContent = true
```

This **pins the current effective inherited behaviour** (recorded on S1 before the change:
`submit_type = MERGE_IF_NECESSARY`, `use_content_merge` inherited `true`) so the atomic
`--no-ff` merge-back cannot be silently changed by an All-Projects default edit. It does not
change merge semantics today — it removes the dependency on inheritance.

### 3. `feature-branch-drivers` group + three ACL permission types

A new named group `feature-branch-drivers` holds **three permission TYPES** (AC "three
ACLs"), one of which is applied to two ref-patterns:

| Permission (type)     | Ref pattern                        |
|-----------------------|------------------------------------|
| Create Reference      | `refs/heads/feature/*`             |
| Delete Reference      | `refs/heads/feature/*`             |
| Push Merge Commit      | `refs/for/refs/heads/main`         |
| Push Merge Commit      | `refs/for/refs/heads/feature/*`    |

(Pushing ordinary story changes for review to `refs/for/refs/heads/feature/*` is already
allowed to Registered Users by the inherited/project `refs/for/refs/heads/*` grant — only the
**merge-commit** push is restricted to the group.)

**Membership policy.** Initial members = repository administrators + the named operating
agents that drive feature-branch work. Membership changes only via an
admin-approved `setup-project.sh` edit (the group + its members are provisioned declaratively
and idempotently by the script, which now **creates** the group if absent — the prior
`want`-dict step only *resolved* existing UUIDs). A regular developer is not in the group and
so cannot create `feature/*` branches or push merge commits.

## Cost & latency

The §1 asymmetry (LLM-Review carries, Verified re-runs across `MERGE_FIRST_PARENT_UPDATE`) is a
deliberate cost trade: it avoids redundant LLM re-reviews while accepting the CI re-runs safety
requires. Measured on the S5 live runs (story leafy-vogue-ingot):

- **CI wall-clock per merge/change ≈ 830–849 s (~14 min) median** (S5 `gerrit-verify` runs,
  e.g. changes 251 / 252 / 254). This matches the `test.yml` matrix latency and is the
  **dominant measured cost** of the flow — every re-merge pays it once (Verified re-runs).

- **LLM re-reviews avoided vs CI re-runs incurred.** On a first-parent-only re-merge,
  `LLM-Review` **carries** (0 re-reviews) while `Verified` **re-runs** (1 CI run) — proven by
  S3 AC4 (change 234 PS3) and S5 scenario 4. A merge-back does **not** re-review the stories:
  the bot reviews only the auto-merge delta (S5 scenario 3, change 254, showed it reviewing
  `/MERGE_LIST`, not the per-story files). So N accumulated stories cost **N per-story reviews
  + 1 merge-change review**, versus re-reviewing the whole combined diff on every re-merge —
  the carry is what makes the reviewed-merge pattern cheaper than a squash-and-re-review.

- **Per-review LLM cost is not separately surfaced.** The review bot logs no per-call token or
  dollar figure, so none is quoted here — a fabricated number would be worse than an honest
  gap. CI wall-clock above is the concrete measured cost; the LLM saving is counted in
  **re-reviews avoided**, not dollars.

## Consequences / back-out

- **copyCondition back-out is inert to remove.** `MERGE_FIRST_PARENT_UPDATE` cannot match a
  non-merge patchset, so the added token is a no-op absent merge changes — no label/gate
  change is REQUIRED to disable the feature-branch flow; the token may be left or reverted.
- **Submit-type back-out** = delete the `[submit]` block to restore `INHERIT` (which currently
  resolves to `MERGE_IF_NECESSARY` + content-merge `true`, the S1-recorded prior state).
- **Group RETIRE path** (feature-branch pattern deprecated): revoke the three ACL grants in
  `project.config`, push refs/meta/config, then delete/empty `feature-branch-drivers` so no
  lingering privileged group or stale ACL remains. (Live back-out is tested in S6.)
- **ACL enforcement is Gerrit-native**: a non-member merge push or `feature/*` creation is
  refused by Gerrit server-side and recorded in Gerrit's sshd/httpd audit log — the review-bot
  is not in that path, so no rebar-side metric is added; the audit log is the "signal to
  watch" (documented in the S4 runbook).
