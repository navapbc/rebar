# Contributing to rebar

rebar dogfoods its own premise: **every change to `main` is gated by two independent
deterministic checks before it can land** — an LLM code review **and** CI. Contributions
flow through a self-hosted **Gerrit** server (`https://rebar.solutions.navateam.com`),
where two bots vote on your change: the **rebar review-bot** casts **`LLM-Review`** (the
LLM code review) and **CI** casts **`Verified`** (build/test/lint/typecheck, run on GitHub
Actions). **Both must be `+1` to submit** — neither a human nor either bot can bypass the
other. **GitHub is a read-only mirror** — `main` there only advances when a
Gerrit-submitted change replicates out. Direct pushes and pull-request merges to GitHub
`main` are rejected.

> **TL;DR of the loop:** clone from Gerrit → install the `commit-msg` hook → commit →
> `git push origin HEAD:refs/for/main` → the bots vote `LLM-Review` (LLM) and `Verified`
> (CI) → fix findings and re-push the amended commit until **both** are `+1` (comment
> `recheck` to re-run CI) → **Submit** → it replicates to GitHub `main`.

> **Status.** Both votes are **live and blocking today**: a change needs `LLM-Review = +1`
> **and** `Verified = +1` to submit. (The `Verified`/CI requirement was activated
> 2026-07-02. If CI infra breaks, an operator can temporarily back out to single-vote
> `LLM-Review`-only gating so `main` isn't frozen — see
> `infra/runbooks/two-vote-gate-rollback.md`.)

If you only read the code (no contributions), just use the GitHub mirror as usual — you
don't need Gerrit.

> **First time contributing? Start with the friendly walkthrough:
> [docs/your-first-change.md](docs/your-first-change.md).** It walks you through one
> change end to end (account → clone → commit → push → votes → submit). This document
> is the complete reference behind that tutorial.

---

## 1. One-time setup

### 1a. Get a Gerrit account + credentials
1. Open **https://rebar.solutions.navateam.com** and click **Sign in**. You'll be
   redirected to **GitHub** to authorize (auth is GitHub OAuth — use your GitHub
   identity). After authorizing you land back in Gerrit as your account.
2. Generate an **HTTP password** for git-over-HTTP: **Settings → HTTP Credentials →
   Generate new password**. Copy it — this is your git password (your username is shown
   on the same page). *(Prefer SSH? Add a public key under Settings → SSH Keys and use
   the `ssh://…:29418/rebar` remote instead.)*

### 1b. Clone from Gerrit and install the Change-Id hook
```bash
# Clone from Gerrit (authenticated remote — the /a/ prefix forces login).
git clone "https://<your-gerrit-username>@rebar.solutions.navateam.com/a/rebar"
cd rebar

# REQUIRED: the commit-msg hook stamps each commit with a Change-Id trailer, which is
# how Gerrit tracks a change across re-pushes. Without it, your push is REJECTED.
curl -Lo .git/hooks/commit-msg \
  "https://rebar.solutions.navateam.com/tools/hooks/commit-msg"
chmod +x .git/hooks/commit-msg
```
Git will prompt for your HTTP password on the first authenticated fetch/push; use a
credential helper (`git config --global credential.helper store`/`osxkeychain`) so you
aren't re-prompted.

Set up the local dev env per [`docs/local-dev-env.md`](docs/local-dev-env.md) (run
`make install`). Note these are **two independent git hooks** that do not conflict:
`make install` wires the check-only **`pre-commit`** hook (lint/format, `.git/hooks/pre-commit`),
while the step above installs the Gerrit **`commit-msg`** hook (Change-Id stamping,
`.git/hooks/commit-msg`) — different hook files, safe in either order.

---

## 2. The contribution loop

### 2a. Make a change and commit
Work on a local branch as usual, then commit. The `commit-msg` hook adds a `Change-Id:`
trailer automatically:
```bash
git checkout -b my-change
# … edit, then …
git commit -m "component: what changed and why"
git log -1   # confirm a "Change-Id: I…" line is present in the footer
```

**Every commit must reference a rebar ticket.** CI's `Verified` gate rejects a commit to
`main` whose message does not reference a rebar ticket that resolves in the store — via a
`rebar-ticket: <id>` trailer (preferred) or a leading `<id>:` subject line. `<id>` may be an
alias, full id, short id, or Jira key. See
[docs/commit-ticket-trailer.md](docs/commit-ticket-trailer.md).

```bash
git commit -m "component: what changed and why

rebar-ticket: blank-guild-koi"
```

### 2b. Push for review
Push to the magic `refs/for/main` ref — this creates (or updates) a Gerrit **change**,
it does **not** touch `main`:
```bash
git push origin HEAD:refs/for/main
```
Gerrit prints a change URL (`…/c/rebar/+/<number>`). Open it.

### 2c. The gate: two votes (`LLM-Review` + `Verified`)
Two bots review your patchset independently and each casts one vote. Your change is
**submittable only when both are `+1` and nothing is unresolved**:

> `label:LLM-Review = MAX (+1)` **AND** `label:Verified = MAX (+1)` **AND** there are **no
> unresolved comments**.

- **`LLM-Review`** — the rebar review-bot's LLM code review of your diff.
- **`Verified`** — CI (the same build/test/lint/typecheck suite as the GitHub `test.yml`,
  run on GitHub Actions via gerrit-to-platform) against your exact patchset. `+1` = CI
  passed, `-1` = CI failed. A run link is posted with the vote.

The two votes are **independent**: an LLM finding does not fail CI, and a CI flake does not
change the review verdict. **Only the two bots and administrators may cast either label**,
so you cannot self-approve or self-verify your own change. (Both labels block submit today —
the `Verified` requirement was activated 2026-07-02; see the status note above.)

**Reading a `-1` (quick version).** An `LLM-Review` `-1` is either a **finding** in
your code (`[LLM-Review: BLOCK — finding]`, with inline comments → fix, amend, re-push,
§2d) or a **coverage-gap** infra veto (`[LLM-Review: BLOCK — coverage-gap (…)]` → the
review couldn't fully run; **not your code** — a maintainer re-triggers it once infra
recovers, don't "fix" your diff). A `Verified` `-1` means CI failed: open the linked
run, fix a real failure and re-push (§2d), or comment **`recheck`** to re-run CI on the
same patchset for a flake.

> **Full vote semantics live in one place:** [docs/review-policy.md](docs/review-policy.md)
> has the complete tag table (every coverage-gap sub-reason and the merge-change
> variants, transcribed from the code), who may vote, the dispute / override path, and
> the responsibility clause. This §2c is the in-flow summary; that doc is authoritative.

### 2d. Address findings and re-push
Amend the **same** commit (keep the `Change-Id` so Gerrit updates the existing change
rather than opening a new one) and push again:
```bash
# … fix the findings …
git add -A
git commit --amend --no-edit    # keeps the Change-Id trailer
git push origin HEAD:refs/for/main
```
Each push is a new **patchset**. Both bots re-run: the review-bot re-reviews (LLM) and CI
re-runs and re-votes `Verified`. Resolve any inline comments (mark them **Done**/resolved)
so `-has:unresolved` is satisfied.

### 2e. Submit
Once **both** `LLM-Review` and `Verified` are `+1` and no comments are unresolved, click
**Submit** on the change page (or `ssh -p 29418 <you>@rebar.solutions.navateam.com gerrit review --submit <change>,<patchset>`).
Gerrit merges the change into its `main`, then **replicates the new `main` to GitHub** —
where the same commit appears on the read-only mirror (and the branch CI runs on the
push). That replication is the only way GitHub `main` advances.

> **`main` is Fast Forward Only (ADR-0040): a change must sit on the current tip to submit.**
> Gerrit will not merge or rebase for you. If `main` moved while you were in review, Submit is
> refused ("not fast-forward / out of date") until you put your change back on the tip:
> `git fetch origin && git rebase origin/main` (a **feature-branch** change re-merges: `git
> merge --no-ff gerrit/feature/<name>` onto the new `main`), then re-push
> `HEAD:refs/for/main`. The rebase drops the stale `Verified` vote and **CI re-runs against the
> exact tree that lands**, which is what guarantees a change that breaks CI can never reach
> `main`. Expect to do this under concurrent landing — it is the deliberate tradeoff (no
> auto-merge of an untested tree). Do **not** add `changekind:TRIVIAL_REBASE` to the `Verified`
> `copyCondition`: that would let a stale vote survive a rebase and defeat the guarantee.

> **Submitting requires contributor authorization.** The **Submit** action is restricted to
> the `Contributors` group (plus Administrators) — anyone may push to `refs/for/*` to
> *propose* a change, but only an authorized contributor (or an admin) can *land* one, even
> when both votes are `+1`. If Submit is unavailable to you, ask an admin to add your Gerrit
> account to the `Contributors` group (or to submit on your behalf). This is enforced
> natively by the Submit ACL in `infra/gerrit/project.config` (managed by
> `infra/gerrit/setup-project.sh` via `CONTRIBUTOR_MEMBERS`).

---

## Sign your work (DCO)

rebar uses the **Developer Certificate of Origin** (<https://developercertificate.org>)
as its inbound-contribution agreement — **the DCO, not a CLA**. Signing off certifies
that you wrote the change (or have the right to submit it) under the project's license;
it requires **no paperwork**, just a trailer on each commit.

**Add the sign-off with `git commit -s`.** It appends a line with your real name and
email:

```
Signed-off-by: Your Name <you@example.com>
```

Use your **real name** (pseudonyms don't satisfy the DCO), and an email you can be
reached at. This is **enforced at push time**: Gerrit rejects an unsigned push to
`refs/for/*`, so a commit without a `Signed-off-by:` trailer cannot even reach review.

**Fixing a missing sign-off.** If a push is rejected (or you forgot), re-sign and
re-push — nothing is lost:

```bash
git commit --amend -s --no-edit      # sign the tip commit
git rebase --signoff origin/main     # or sign a whole branch of commits
git push origin HEAD:refs/for/main
```

**Shepherded patches carry two sign-offs.** When a maintainer shepherds your patch
(see §3a), your own `Signed-off-by:` must already be in the patch (you certify your
work); the shepherd adds **theirs** on amend. Both lines stay in the final commit.

> **Why DCO and not a CLA.** A CLA asks contributors to grant rights beyond the
> license and is a barrier peers explicitly cite for *not* accepting contributions;
> the DCO lowers that barrier while still establishing provenance. **rebar would adopt
> a CLA only if Nava counsel requires rights beyond Apache-2.0 §5 + the DCO.**

> **Post-flip failure runbook (maintainers).** A rejected push after the DCO flip →
> `git commit --amend -s --no-edit` and re-push. If agent/bot tooling regresses because
> its commits aren't signed, **roll back the flag** (`requireSignedOffBy` in
> `infra/gerrit/project.config`, then re-run `setup-project.sh`) while you fix the
> tooling — see that file's rollback note.

## 3. GitHub is a read-only mirror

After the cutover, `navapbc/rebar` on GitHub is a **mirror**: `main` only advances via
Gerrit's replication identity. **Direct `git push` to GitHub `main` and PR merges are
rejected by a repository ruleset** — there is no human merge path on GitHub. Open your
changes in Gerrit, not as GitHub PRs. (Reading, cloning, issues, and CI on the mirror all
keep working; tags are not locked, so releases still publish normally.)

> **Maintainers — emergency escape hatch.** If the Gerrit path is broken and `main` is
> frozen (Gerrit/bot down, replication failing, or an urgent hotfix), the mirror lock can
> be rolled back — see the **when-to-roll-back trigger** and the fast temporary-bypass
> un-lock in [`infra/runbooks/github-mirror-lock.md`](infra/runbooks/github-mirror-lock.md).
> A single rejected human push is the lock working as intended, not a reason to roll back.

> **Changing a public surface?** rebar is 0.x, but its public surfaces have
> differing stability guarantees — the `--output json` schemas and the event wire
> format are compatibility-bearing even pre-1.0. Before you change a CLI flag, a
> JSON schema, a `rebar.*` signature, an MCP tool, an event type, or a config key,
> read [docs/api-stability.md](docs/api-stability.md) and follow the
> deprecate-then-remove rule it documents.

### 3a. Shepherded patches (when you can't use Gerrit yourself)

If setting up Gerrit is a barrier — or you opened a GitHub PR and the bot redirected
you here — a maintainer can **shepherd your patch** onto Gerrit for you. This path is
deliberately **best-effort and slower** than pushing your own change (a human has to
pick it up), but it means a good patch never goes to waste.

**How it works:**

1. **You** make your change and commit it with your own DCO sign-off:
   `git commit -s` (this adds your `Signed-off-by:` line — see the DCO section). Then
   export the patch: `git format-patch -1`. Attach the resulting `.patch` file to a
   GitHub issue describing the change. (A plain diff with your name and email is also
   fine, as long as we can credit you.)
2. **A maintainer** applies it with `git am`, which **preserves you as the commit
   author** (author = you, committer = the shepherd). Gerrit permits this because it
   grants Forge Author to registered users, so your authorship is kept intact — you
   remain the author of record.
3. **The maintainer** amends the message only (authorship untouched) to add their own
   `Signed-off-by:` line and a `rebar-ticket: <id>` trailer (they create or reuse the
   ticket), then pushes it for review and drives it through the two votes on your
   behalf. If the patch is substantially rewritten during review, they add a
   `Co-authored-by:` trailer so credit is shared.

So a shepherded patch carries **two `Signed-off-by:` lines** (yours + the shepherd's)
and keeps you as the author. It is a genuine best-effort convenience, not a fast lane —
if you can, the self-serve [tutorial](docs/your-first-change.md) is quicker.

---

## 4. Multi-story features (feature branches)

The single-change loop in §2 is the right path for **one** self-contained change. A
larger feature that spans **several stories** — especially when multiple agents work it
in parallel — lands instead through a **server-side feature branch**: stories are
reviewed *into* `refs/heads/feature/<name>` (each passing both gates), and the whole
branch is then merged into `main` by a **single reviewed `--no-ff` merge change** that is
gated identically and submitted atomically. `main` never sees a half-finished feature,
and each story still gets its own two-vote review. See
[ADR-0025](docs/adr/0025-feature-branch-merge-carry.md) for the design.

> **When to use this.** Reach for a feature branch only when a feature is genuinely
> multi-story (or multi-agent). A single small change does **not** need one — just push
> it to `refs/for/main` per §2. The feature branch buys you an integration point off
> `main`; it also costs an extra reviewed merge change, so don't pay for it on a one-shot
> fix.

### 4a. Prerequisite — you must be a feature-branch driver

Creating a `feature/*` branch and pushing the merge commit are restricted to the
**`feature-branch-drivers`** Gerrit group (ADR-0025): only its members hold *Create
Reference* / *Delete Reference* on `refs/heads/feature/*` and *Push Merge Commit* on
`refs/for/refs/heads/main` and `refs/for/refs/heads/feature/*`. **Pushing ordinary story
changes for review into a feature branch needs no special membership** — the inherited
`refs/for/refs/heads/*` grant already allows any registered user to do that. Only branch
*creation* and the *merge-commit* push are gated.

If you are not a member, ask a repository administrator to add you (membership is
provisioned declaratively via `setup-project.sh` / `FEATURE_BRANCH_DRIVER_MEMBERS`; see
ADR-0025). A non-member create/merge-push is refused by Gerrit server-side.

### 4b. Create the feature branch (driver, one-time per feature)

A driver creates the branch off the current `main` tip, either in the Gerrit UI
(*Browse → Repositories → rebar → Branches → Create*) or over SSH:

```bash
ssh -p 29418 <you>@rebar.solutions.navateam.com \
  gerrit create-branch rebar refs/heads/feature/<name> main
```

Pick a short `<name>` (e.g. `feature/login-epic`). Everyone working the feature branches
their local work from it.

### 4c. The story loop — review each story INTO the feature branch

Work each story exactly like §2, except the review target is the **feature branch's**
magic ref, not `main`:

```bash
git fetch gerrit
git checkout -b my-story gerrit/feature/<name>
# … edit, commit (the commit-msg hook stamps a Change-Id; every commit needs a
#     rebar-ticket trailer per §2a) …
git push gerrit HEAD:refs/for/refs/heads/feature/<name>
```

Each story is a normal Gerrit change and gets the **full two-vote gate** (`LLM-Review` +
`Verified`) against the feature branch. Submit each story once both are `+1` and nothing
is unresolved; it merges into `feature/<name>`, not `main`.

### 4d. Catch-up merge — keep the feature branch current with `main`

While the feature is in flight `main` moves. Periodically merge `main` **into** the
feature branch so stories review against current code (and so the final merge-back has
fewer conflicts). A driver does this and pushes it for review like any other change:

```bash
git fetch gerrit
git checkout -b catchup gerrit/feature/<name>
git merge gerrit/main           # resolve conflicts if any (see §4f), then commit
git push gerrit HEAD:refs/for/refs/heads/feature/<name>
```

This is itself a change on the feature branch — it goes through both gates and is
submitted normally.

### 4e. Merge-back — land the whole feature into `main` (driver)

When every story has landed on `feature/<name>`, a driver opens the **single `--no-ff`
merge change** that integrates the branch into `main`.

**Prerequisite — install the `commit-msg` hook in THIS checkout first.** A merge commit
needs a `Change-Id` just like any other change, and a **fresh worktree/clone does not have
the hook** — if it is missing, the merge push is rejected with *missing Change-Id* (§6).
Install it before you create the merge commit:

```bash
curl -sLo .git/hooks/commit-msg \
  https://rebar.solutions.navateam.com/tools/hooks/commit-msg
chmod +x .git/hooks/commit-msg
```

Then create the no-fast-forward merge and push it to `refs/for/main`:

```bash
git fetch gerrit
git checkout -b merge-<name> gerrit/main
git merge --no-ff gerrit/feature/<name>   # resolve conflicts if any (§4f)
# The commit-msg hook should have stamped a Change-Id. If it did NOT (hook was
# installed only after the merge commit was made), re-stamp WITHOUT re-editing:
GIT_EDITOR=/bin/true git commit --amend
git log -1   # confirm a "Change-Id: I…" line is present

git push gerrit HEAD:refs/for/main
```

This merge change is gated **identically** to any other: `LLM-Review` + `Verified` must
both be `+1`. The `LLM-Review` bot reviews the auto-merge delta; CI runs `Verified`
against the merge tree. Submit once both are green — Gerrit lands the whole feature on
`main` atomically and replicates it to the GitHub mirror.

**Re-merge behaviour when `main` advances under your open merge change (ADR-0025).** If
`main` moves while the merge change is in review, re-merge to refresh it:

```bash
git fetch gerrit
git merge --no-ff gerrit/main            # brings your merge change's first parent up to date
GIT_EDITOR=/bin/true git commit --amend  # keep the Change-Id
git push gerrit HEAD:refs/for/main
```

This produces a `MERGE_FIRST_PARENT_UPDATE` patchset (first parent moved, reviewed
feature tip unchanged). **`LLM-Review` carries** across it (the reviewed delta is
identical) but **`Verified` re-runs** (a new merge tree must be re-built by CI). So expect
CI to run again but the LLM vote to stick. **Changing the feature tip itself is REWORK, not
`MERGE_FIRST_PARENT_UPDATE` — it drops *both* votes and forces a full fresh review.**

### 4f. Resolving merge conflicts

Both the catch-up (§4d) and merge-back (§4e) merges can conflict. Resolve them the normal
git way — there is nothing Gerrit-specific:

```bash
git merge --no-ff gerrit/feature/<name>
# … git reports conflicts …
git status                 # list conflicted paths
# edit each file to resolve, then:
git add <resolved-paths>
git commit                 # completes the merge; the commit-msg hook stamps a Change-Id
```

Keep the resolution commit as the merge commit (don't flatten it into a squash — the
`--no-ff` merge topology is what makes the feature land atomically). If the hook did not
stamp a `Change-Id` (e.g. you resolved with `git merge --continue` before installing it),
re-stamp with `GIT_EDITOR=/bin/true git commit --amend` (§4e).

### 4g. Abandon a bad merge change and start over

If a merge change is wrong (bad conflict resolution, wrong parent, stale feature tip) and
you'd rather restart than amend it:

1. **Abandon the change in Gerrit** — on the change page click **Abandon**, or
   `ssh -p 29418 <you>@rebar.solutions.navateam.com gerrit review --abandon <change>,<patchset>`.
   Abandoning affects only the review change; it does **not** touch `main` or the feature
   branch.
2. **Redo the merge from a clean base** and push a fresh change:
   ```bash
   git fetch gerrit
   git checkout -B merge-<name> gerrit/main
   git merge --no-ff gerrit/feature/<name>   # resolve conflicts (§4f)
   git push gerrit HEAD:refs/for/main         # a NEW Change-Id ⇒ a new change
   ```
   (Because you started from a fresh checkout the commit-msg hook mints a new `Change-Id`,
   so this opens a new change rather than updating the abandoned one.)

The feature branch itself is untouched — only the merge *change* is replaced.

### 4h. Branch lifetime & catch-up cadence — keep feature branches short-lived

Feature branches are a **short-lived integration buffer for one multi-story feature**, not a
place for sustained parallel development. The pattern's own sources are explicit about this:
OpenDev documents server-side feature branches as **"not for sustained long-term
development"**, and Qt **abandoned routine long-lived-branch merges** because the recurring
catch-up/merge-back cost outgrew the benefit. A branch that lingers accrues conflict debt
against a fast-moving `main` and dilutes the atomic merge-back guarantee.

**Catch-up cadence (drivers).** Merge `main` into the feature branch (§4d) **at least every
few days while the feature is in flight, and before starting each new story** on it, so every
story is reviewed against current code and the final merge-back stays small. Don't let a
branch drift more than a handful of `main` advances behind.

**Lifetime cap.** Treat **14 days of inactivity** (no new story landed, no catch-up merge) as
the point to either finish the merge-back (§4e) or abandon the branch. Gerrit does **not**
auto-prune merged or stale `feature/*` refs — a driver must delete them explicitly (Delete
Reference is a `feature-branch-drivers` grant; ADR-0025), so stale branches accumulate until
someone cleans them up.

**Inventory the branches.** `infra/gerrit/feature-branch-inventory.sh` lists the live
`feature/*` refs, classifies each as **merged-back** (already integrated into `main`) vs
**abandoned**, and flags any inactive beyond the 14-day cap — run it periodically and delete
what it surfaces (owner-confirmed). See `infra/runbooks/review-bot-ops.md` for the ops view.

---

## 5. Supply-chain security & dependency updates

rebar runs two supply-chain checks in CI, and both produce **alerts only** — there
is **no automated fix-PR path**, because GitHub PRs cannot merge here (the tree is
Gerrit-gated; see §3). Any resulting fix lands as an ordinary Gerrit change (§2).

- **CodeQL SAST** (`.github/workflows/codeql.yml`) statically analyses the Python
  source and uploads findings to the repo's **Security → Code scanning** alerts
  tab. It runs on push to `main` (the post-merge tree), on mirror PRs, and weekly.
- **`pip-audit`** scans rebar's installed dependency closure against the PyPI/OSV
  advisory database. It is **gating**: a known vulnerability with a fix fails
  `Verified` (it runs in both branch CI `test.yml` and the Gerrit `gerrit-verify`
  gate). An accepted/unfixable advisory is silenced in-workflow with
  `--ignore-vuln <ID>` plus a justification — never a blanket skip. A transient
  advisory-DB fetch error is retried and, if still failing, is an infra issue
  (comment `recheck`), not a vulnerability.

**Dependency updates use security *alerts only*, not version-bump PRs.** GitHub
Dependabot security **alerts** are enabled to surface vulnerable deps, and
`.github/dependabot.yml` configures **GitHub-Actions version-update PRs** (monthly).
Because PRs cannot merge on the mirror, these are **advisory notifications**: a
maintainer reads the proposed bump, lands the equivalent change through Gerrit, and
closes the PR (the lockdown bot exempts `dependabot[bot]` so its PRs aren't
auto-closed). There is intentionally **no `pip` ecosystem entry** — rebar's core
deps are unpinned `>=`, so version-update PRs there would be noise. When an alert
(or a `pip-audit` failure) tells you to bump a dependency, **land the bump through
Gerrit** like any other change (§2): edit the pin in `pyproject.toml`, commit with
a `rebar-ticket:` trailer, and push to `refs/for/main`.

> **Reporting a vulnerability.** See [`SECURITY.md`](SECURITY.md) for private
> disclosure — do not open a public issue for a security report.

---

## 6. Troubleshooting

- **`missing Change-Id in commit message footer` on push.** The `commit-msg` hook isn't
  installed (or wasn't installed when you committed). Install it (§1b), then re-stamp the
  existing commit with `git commit --amend --no-edit` and push again. To fix a whole
  series, `git rebase -i` and reword, or re-commit.
- **`! [remote rejected] … (prohibited by Gerrit: not permitted: create)` pushing to
  `refs/heads/…`.** You pushed to a branch instead of the review ref. Push to
  **`HEAD:refs/for/main`**, not `HEAD:main`.
- **HTTP 401 on fetch/push.** Your HTTP password is missing/expired. Regenerate it under
  **Settings → HTTP Credentials** and update your credential helper.
- **A `-1` tagged `coverage-gap`.** Infra, not your code — see §2c. Re-push once the
  review infra is healthy; don't change your diff to chase it.
- **My change won't submit even at `LLM-Review +1`.** Check that **`Verified` is also
  `+1`** (both votes are required — see §2c) and that there are **no unresolved comments**
  (the submit rule is `LLM-Review=MAX AND Verified=MAX AND -has:unresolved`); mark comments
  resolved.
- **CI (`Verified`) didn't run / no run appeared.** The CI dispatch (Gerrit →
  gerrit-to-platform → GitHub Actions) may be down. Comment **`recheck`** to re-trigger; if
  still nothing, it's an infra issue for maintainers (see
  `infra/runbooks/two-vote-gate-rollback.md`) — not a problem with your diff.
- **`Verified -1` but the failure looks transient/flaky.** Comment **`recheck`** to re-run
  CI on the same patchset (§2c). A new patchset also re-runs it and cancels the stale run.
- **`! [remote rejected] … you are not allowed to upload merges` (or `not permitted:
  push merge commit`) pushing the merge-back.** Pushing a merge commit to
  `refs/for/refs/heads/feature/*` or `refs/for/main` is restricted to the
  **`feature-branch-drivers`** group (§4a, ADR-0025). Ordinary (non-merge) story pushes
  are unaffected — only the `--no-ff` merge push is gated. Ask an administrator to add you
  to the group, or have a driver push the merge change.
- **`missing Change-Id in commit message footer` on the merge push.** The `commit-msg`
  hook isn't installed in this checkout — a **fresh worktree/clone does not carry it** (§4e).
  Install it (§1b / §4e), then re-stamp the existing merge commit **without re-editing** the
  message so you keep the merge as-is: `GIT_EDITOR=/bin/true git commit --amend`, confirm a
  `Change-Id: I…` line with `git log -1`, and push again.

## 7. Editing the plan-review reviewer prompts (affirmative-framing habit)

The plan-review gate critiques prompt hygiene in the plans it reviews (criterion T8:
instruction-locality, the pink-elephant/negative-priming antipattern), so its **own** reviewer
prompts under `src/rebar/llm/reviewers/plan_review_*.md` must hold themselves to the same bar
(gap-report R-6, epic `cite-stone-sea` / WS7). When you add or edit one of those prompts, apply
this **review-time checklist**:

- **Lead with the affirmative** — say what the reviewer SHOULD do first; keep any genuinely-needed
  prohibition terse and put the "do this instead" redirect right next to it (e.g. "score the
  flaw's own reach — a wide blast radius never *raises* a trivial finding's severity").
- **No bare DO-NOT-only blocks** — never leave a bullet or paragraph whose only content is a
  prohibition with no adjacent affirmative. Don't narrate failure mechanics at length; the
  cross-cutting stance (material-vs-instruction trust boundary, the forward-looking rule) already
  lives once in the shared preamble (`_SHARED_PREAMBLE` in `src/rebar/llm/plan_review/passes.py`),
  injected into every pass system prompt by `_resolve_system` — don't re-derive it per prompt.

This is enforced deterministically by `tests/unit/test_reviewer_prompt_hygiene.py`
(`test_no_bare_do_not_only_blocks`), which runs in CI — a re-runnable guard, not a hand checklist.

---

Track your work in rebar (see [`CLAUDE.md`](CLAUDE.md) and [`docs/`](docs/)); the Gerrit
server + review-bot architecture is documented in
[`docs/gerrit-aws-setup.md`](docs/gerrit-aws-setup.md).
