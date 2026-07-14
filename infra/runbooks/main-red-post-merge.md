# Runbook — `main` is red after a merge (post-merge CI + manual revert) (ADR-0047)

Since [ADR-0047](../../docs/adr/0047-retire-autolander-rebase-if-necessary.md) retired the
auto-lander and switched `main` to Gerrit **Rebase-If-Necessary**, a change is submitted the
moment it has `LLM-Review +1` **and** `Verified +1` and is mergeable — and a *clean*
(non-conflicting) server-side rebase is submitted **without** re-running integration CI on the
integrated tree. That is a deliberate, cost-driven relaxation (see ADR-0047 §Decision): the
rare price is a **semantic conflict** — two individually-green changes that break only once
combined on `main`. This runbook is the safety net for that case.

## The detector — post-merge `main` CI (free, already running)

- **What runs.** `.github/workflows/test.yml` triggers on **`push`** to every branch except
  `feature/**` (`branches-ignore: ["feature/**"]`). When Gerrit submits a change it replicates
  the new `main` to the GitHub mirror (`navapbc/rebar`); that push fires `test.yml`, which runs
  the **same full suite** the `Verified` gate runs (`make lint && make typecheck && make test`,
  plus the module-size and docs-index gates).
- **Where to watch it.** GitHub → Actions → the **CI** workflow, runs on branch `main`
  (`https://github.com/navapbc/rebar/actions?query=branch%3Amain`).
- **What it gates.** Nothing *pre-merge* — the change has already landed on `main` by the time
  this runs. It is a **detector**, not a gate: a red run here is the signal that a
  semantic conflict (or any escaped breakage) reached `main`, and the remedy is a **manual
  revert** (below). This is the accepted bounded risk of ADR-0047 (~26 merges/day; the window
  between a bad submit and the red signal is one CI run).

## When `main` goes red — manual revert

1. **Confirm it's real, not a flake.** Open the failing `main` CI run. If it looks like a known
   flake (see `infra/runbooks/two-vote-gate-rollback.md` for the CI-flake posture), re-run the
   job first; only proceed if it reproduces. A genuine semantic-conflict failure reproduces
   deterministically on `main`.
2. **Identify the culprit change.** The red run's commit is the current `main` tip. Walk back
   from it: `git log --oneline origin/main` and correlate with the most recently submitted
   Gerrit change(s). A semantic conflict usually implicates the **last one or two** submits (the
   pair that combined badly); the failing test names the subsystem.
3. **Create the revert in Gerrit.** Revert the culprit change through Gerrit so the revert is
   itself a reviewable, gated change (do **not** push to GitHub `main` — it is a read-only
   mirror):
   - In the Gerrit UI, open the culprit change and use **Revert** (it creates a revert change
     with a `Change-Id` and a `This reverts commit …` message), **or** locally:
     `git fetch origin && git revert <culprit-sha>`, add a `rebar-ticket:` trailer + DCO
     `Signed-off-by`, then `git push gerrit HEAD:refs/for/main`.
4. **Land the revert.** It goes through the normal two-vote gate — `LLM-Review +1` **and**
   `Verified +1` — then **Submit** it (plain Gerrit Submit; `main` is Rebase-If-Necessary, so
   Gerrit rebases + submits server-side). Reverts are typically clean and fast to verify.
5. **Confirm `main` is green again.** Watch the post-merge `main` CI run on the revert's push
   settle green.
6. **Re-land correctly.** Reopen/track the reverted work under its rebar ticket, fix the
   semantic conflict (the two changes must be made compatible), and re-submit through the normal
   flow.

## Why there is no auto-revert

Auto-revert (a bot that watches `main` CI and reverts on red) is **deliberately NOT built** —
it is added infrastructure that runs counter to the ADR-0047 cost/scope reduction (the whole
point of the epic was to *remove* a stateful landing bot, not add another). At the current merge
rate a manual revert is a rare, low-cost operation. **Revisit only if red-`main` recurs** often
enough to justify the automation.

## See also

- [ADR-0047](../../docs/adr/0047-retire-autolander-rebase-if-necessary.md) — the decision +
  the bounded-risk rationale.
- `infra/runbooks/two-vote-gate-rollback.md` — the pre-merge two-vote gate + CI-flake posture.
- [CONTRIBUTING.md](../../CONTRIBUTING.md) §2e — the normal Submit-when-green landing flow.
