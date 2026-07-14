# ADR 0047 — Retire the auto-lander; land via Gerrit Rebase-If-Necessary + agent Submit

**Status:** Accepted
**Date:** 2026-07-13
**Supersedes:** the lander portions of **ADR 0040** (`main` Fast-Forward-Only submit type)
and **ADR 0042** (serial auto-lander). Relaxes ADR 0040's requirement **R5** (pre-merge
"impossible to land a stale/untested tree"). ADR 0041 (LLM-Review carries `TRIVIAL_REBASE`)
becomes moot for landing once there is no client-side rebase step.
**Epic:** `d4de-7d65-2a96-48be` (`fleshy-kimberlite-crayfish`).

## Context

ADR 0040 made `main` **Fast-Forward-Only (FFO)** to guarantee that the CI-tested tree is
byte-for-byte the tree that lands (R5), motivated by a real semantic-conflict incident (two
individually-green changes disagreeing about the `IDEA` Jira status produced a red `main`).
FFO's accepted cost (R4) was a **rebase-treadmill**: when `main` advances, every in-review
change goes non-submittable until rebased-and-re-CI'd. ADR 0042 built a **single-instance
serial auto-lander** to automate that treadmill so agents could "set `Autosubmit` and walk
away."

In practice the auto-lander became a sustained source of defects. Root cause: **it is a
stateful, polling, client-side bridge that re-implements Gerrit/git integration to reconcile
Gerrit's *synchronous* submit gate with GitHub Actions' *asynchronous* CI across a *mandatory
rebase*.** That mandatory client-side rebase exists **only** because of FFO — Gerrit's config
reference is explicit that Fast-Forward-Only is the one submit type that will **not**
rebase/merge for you, so an external actor must
([Gerrit project-config: submit types](https://gerrit-review.googlesource.com/Documentation/config-project-config.html)).
The bridge's bugs clustered into recognizable anti-patterns that mature systems avoid:

| Bug | Anti-pattern |
|---|---|
| `15a1` — FF-reject error-string match → infinite drive loop | control flow driven by **parsing VCS error strings** instead of structured submittability |
| `0d3e`; hand-back cannot clear another account's vote | a **review label used as durable queue state** (non-sticky across rebase; not removable by the bot under Gerrit's ACL) |
| TOCTOU rebase churn; "both votes green ≠ it lands" | **polling + client-side queue reconstruction** across an async boundary |
| `recovery.json` / drain complexity | a **stateful bot holding in-flight state** instead of a durable queue |
| `dc33` (four successive failures) | **scope creep** — a git-backed rebar ticket-store write bundled into the lander |
| serial treadmill + flake-blocked landings | **no speculative parallelism** (the one thing Zuul / LUCI CV / GitHub merge-queue all have) |

### What the proven systems do, and why we are not adopting them

- **Zuul** (OpenStack) — event-driven, explicit windowed queue, **speculative parallel
  execution**. The throughput-preserving answer, but it *owns CI execution* (Ansible/Nodepool)
  and cannot use GitHub Actions as its runner; adopting it means a ZooKeeper + SQL + scheduler
  + executor + Nodepool control plane — **~3× our monthly hosting cost, unacceptable for a
  POC** ([Zuul gating](https://zuul-ci.org/docs/zuul/latest/gating.html)). Already rejected in
  ADR 0040.
- **Chromium LUCI CV / Commit Queue** — label-triggered, **batches** CLs to amortize CI, but
  it is a horizontally-scaled App-Engine service whose batching only pays off far above our
  ~26 merges/day
  ([LUCI CV](https://chromium.googlesource.com/infra/luci/luci-go/+/refs/heads/main/cv/README.md)).
- **Gerrit native** — has **no** universal "auto-submit when submittable"; the pattern is a
  small per-host bot. But its **Rebase-If-Necessary** submit type makes *Gerrit itself*
  rebase-and-submit server-side, atomically, under its own lock and ACL — no external bridge
  needed.
- **Wikimedia** deliberately moved **off FFO to Merge-If-Necessary**, accepting the "slim
  chance" of a clean-but-semantically-broken merge to escape exactly this rebase hassle
  ([T131008](https://phabricator.wikimedia.org/T131008)).

### Constraints driving the decision

This is a **proof of concept with limited users and scope**, and **cost is a hard
constraint**: GitHub Actions is free for this OSS repo and self-hosting CI would triple
hosting cost. The FFO pre-merge guarantee is the *strongest* posture, but it is **gold-plating
at this scale** — its cost (the bug-generating bridge + redundant re-CI on every rebase) far
exceeds the value of never being *briefly* red on `main`.

## Decision

1. **Relax R5 (pre-merge exactly-tested-tree).** A change that rebases onto the current `main`
   tip **without a textual conflict** may land **without re-running integration CI**. Each
   change is still CI-verified once on its own base; what is dropped is the *re-test after a
   non-conflicted rebase*. The bounded, rare risk — two individually-green changes whose
   *semantic* interaction breaks `main` — is explicitly accepted.
2. **Switch `main` from Fast-Forward-Only to Rebase-If-Necessary.** Gerrit fast-forwards when
   possible, otherwise **rebases the change onto the tip and submits, server-side**, at submit
   time. A *textual* conflict makes Gerrit refuse the submit and hand it back to the author (a
   clean native signal); only non-conflicted integrations land. History stays linear.
3. **Drop the auto-lander entirely.** Delete `infra/autolander/`, the `land` / `land-status`
   command, `docs/land-contract.md`, the container/compose/terraform/nginx/SSM/CloudWatch
   resources, and the Gerrit `Autosubmit` label + rebase-on-behalf ACL. **Agents land by
   running a plain Gerrit Submit** once **`LLM-Review +1` AND `Verified +1`**; Gerrit does the
   rebase. On a textual conflict, the agent does the ordinary `git fetch && git rebase
   origin/main`, re-pushes, re-Submits. No bot, no queue, no label, no walk-away automation —
   judged too expensive to maintain for too little value at this scale.
4. **Post-merge `main` CI is the safety net.** Gerrit already replicates each submit to the
   GitHub mirror, where branch CI runs on `main` — this is the (free) detector for the rare
   semantic conflict. On a red `main`, **revert manually** via Gerrit (runbook to follow).
   **Auto-revert is deliberately NOT built** (added infra vs. POC scope); revisit only if
   red-`main` actually recurs.

The two-vote gate itself is **unchanged**: every change still earns `LLM-Review +1`
(code-review bot) and `Verified +1` (GitHub-Actions CI) on its own base before it can be
submitted. What changes is *who integrates* (Gerrit, not a bot) and *whether the integrated
tree is re-tested before landing* (no, pre-merge; yes, post-merge).

## Consequences

- **Reliability ↑.** Gerrit owns the atomic rebase-and-submit. The entire client-side bug
  surface disappears — error-string parsing, label-as-queue, polling TOCTOU,
  crash-recovery-of-in-flight-state, and the container ticket-store. Bugs `dc33`, `15a1`, and
  `0d3e` are **obsoleted** (deleting the code is their resolution).
- **Cost ↓.** No re-CI on rebases (the treadmill's redundant runs are gone); no lander
  container, volumes, SSM param, or CloudWatch alarm. Net change to CI minutes and infra is
  **negative**.
- **Maintainability ↑.** ~900 LOC of bridge + tests + a deployed service → **zero**. The
  landing flow is a stock Gerrit Submit.
- **Simpler agent model.** The FFO "both votes green ≠ it lands" trap is gone: under
  Rebase-If-Necessary, both-votes-green + mergeable = submittable = it lands.
- **Accepted risk (the tradeoff).** `main` can be *briefly* red when a clean rebase hides a
  semantic conflict, until post-merge CI flags it and a human reverts. At ~26 merges/day with
  a single team and a low conflict rate this is expected to be rare and bounded — the same
  tradeoff Wikimedia accepted.
- **`CI flakes` (`85c3`) remain in scope and valuable** — they are independent of the lander;
  an agent's own Submit still depends on a reliable `Verified` vote.
- **Back-out is one line:** revert the submit type to `fast forward only` on `refs/meta/config`
  (which reinstates the FFO discipline; the deleted bot would then have to be restored to
  re-automate the treadmill — so back-out is realistically "FFO + manual rebase," which is the
  pre-ADR-0042 state).

## Migration (see epic `d4de` children)

Sequenced so the pivotal production flip happens last, and nothing depends on removed pieces
mid-flight; landing is a manual Gerrit Submit throughout:

1. **This ADR** (design record).
2. **Delete the auto-lander code** (`d0e5`) — `infra/autolander/`, `land`/`land-status`,
   `land-contract.md`; close `dc33`/`15a1`/`0d3e` as obsolete when it lands.
3. **Decommission the deployed infra** (`bcf0`) — container, compose, terraform, nginx, SSM
   param, CloudWatch alarm, volumes.
4. **Rewrite the docs** (`5e7a`) — a repo-wide sweep of every `autosubmit` / auto-lander / FFO
   / `land-contract` reference to the Submit-when-green model; supersession banners on ADR
   0040/0042.
5. **Document the post-merge safety net + manual-revert runbook** (`f542`).
6. **Gerrit cutover** (`03c6`) — flip the submit type on `refs/meta/config` and retire the
   `Autosubmit` label + rebase ACL, applied by an admin in an announced quiet window (**done
   last**).
