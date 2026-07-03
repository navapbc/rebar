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

**Reading a `-1`.** An `LLM-Review` `-1` comes in two flavors — check the tag on the bot's
message:

| Bot message tag | Meaning | What to do |
|---|---|---|
| `[LLM-Review: BLOCK — finding]` with inline comments | **Real finding(s)** in your code | Fix the code, amend, re-push (§2d). |
| `[LLM-Review: BLOCK — coverage-gap (llm-unavailable / scanner / gate-disabled / review-error)]` | **Infra veto, not your code** — the review couldn't fully run (LLM down, a scanner failed, the gate was disabled, or a review error). Fail-closed by design. | Not a code problem. Re-trigger the review once the infra recovers (re-push the same commit, or ask an admin). Don't "fix" your diff — there's nothing wrong with it. |

This distinction is deliberate: a coverage-gap `-1` means "we could not prove your change
is safe," not "your change is bad."

**A `Verified` `-1` (CI failed).** Open the linked run to see which check failed, then:

- **Real test/lint/type failure** → fix the code, amend, and re-push (§2d). Each new
  patchset drops the old `Verified` and triggers a fresh run automatically.
- **A flaky/transient failure** (not your code) → comment **`recheck`** on the change to
  re-run CI on the *same* patchset without amending. A new run dispatches and re-votes.
  (You can also just push a new patchset; the in-flight run for the change is cancelled so
  only the newest patchset's run survives.)

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

---

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

---

## 4. Troubleshooting

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

---

Track your work in rebar (see [`CLAUDE.md`](CLAUDE.md) and [`docs/`](docs/)); the Gerrit
server + review-bot architecture is documented in
[`docs/gerrit-aws-setup.md`](docs/gerrit-aws-setup.md).
