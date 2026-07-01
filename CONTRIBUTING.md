# Contributing to rebar

rebar dogfoods its own premise: **every change to `main` is code-reviewed by an LLM
before it can land.** Contributions flow through a self-hosted **Gerrit** server
(`https://rebar.solutions.navateam.com`), where the **rebar review-bot** casts a
single deterministic **`LLM-Review`** vote on your change. **GitHub is a read-only
mirror** — `main` there only advances when a Gerrit-submitted change replicates out.
Direct pushes and pull-request merges to GitHub `main` are rejected.

> **TL;DR of the loop:** clone from Gerrit → install the `commit-msg` hook → commit →
> `git push origin HEAD:refs/for/main` → the bot votes `LLM-Review` → fix findings and
> re-push the amended commit until it's `+1` → **Submit** → it replicates to GitHub `main`.

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

### 2b. Push for review
Push to the magic `refs/for/main` ref — this creates (or updates) a Gerrit **change**,
it does **not** touch `main`:
```bash
git push origin HEAD:refs/for/main
```
Gerrit prints a change URL (`…/c/rebar/+/<number>`). Open it.

### 2c. The `LLM-Review` vote (the gate)
The rebar review-bot reviews your diff and casts a single **`LLM-Review`** vote. Your
change is **submittable only when**:

> `label:LLM-Review = MAX (+1)` **AND** there are **no unresolved comments**.

`LLM-Review` is the **sole** code-review gate in v1 — there is no separate human
Code-Review or CI `Verified` requirement to submit. **Only the review-bot and
administrators may cast `LLM-Review`**, so you cannot self-approve your own change.

**Reading a `-1`.** A `-1` comes in two flavors — check the tag on the bot's message:

| Bot message tag | Meaning | What to do |
|---|---|---|
| `[LLM-Review: BLOCK — finding]` with inline comments | **Real finding(s)** in your code | Fix the code, amend, re-push (§2d). |
| `[LLM-Review: BLOCK — coverage-gap (llm-unavailable / scanner / gate-disabled / review-error)]` | **Infra veto, not your code** — the review couldn't fully run (LLM down, a scanner failed, the gate was disabled, or a review error). Fail-closed by design. | Not a code problem. Re-trigger the review once the infra recovers (re-push the same commit, or ask an admin). Don't "fix" your diff — there's nothing wrong with it. |

This distinction is deliberate: a coverage-gap `-1` means "we could not prove your change
is safe," not "your change is bad."

### 2d. Address findings and re-push
Amend the **same** commit (keep the `Change-Id` so Gerrit updates the existing change
rather than opening a new one) and push again:
```bash
# … fix the findings …
git add -A
git commit --amend --no-edit    # keeps the Change-Id trailer
git push origin HEAD:refs/for/main
```
Each push is a new **patchset**. The bot re-reviews and re-votes. Resolve any inline
comments (mark them **Done**/resolved) so `-has:unresolved` is satisfied.

### 2e. Submit
Once `LLM-Review` is `+1` and no comments are unresolved, click **Submit** on the change
page (or `ssh -p 29418 <you>@rebar.solutions.navateam.com gerrit review --submit <change>,<patchset>`).
Gerrit merges the change into its `main`, then **replicates the new `main` to GitHub** —
where the same commit appears on the read-only mirror (and the branch CI runs on the
push). That replication is the only way GitHub `main` advances.

---

## 3. GitHub is a read-only mirror

After the cutover, `navapbc/rebar` on GitHub is a **mirror**: `main` (and tags) only
advance via Gerrit's replication identity. **Direct `git push` to GitHub `main` and PR
merges are rejected by a repository ruleset** — there is no human merge path on GitHub.
Open your changes in Gerrit, not as GitHub PRs. (Reading, cloning, issues, and CI on the
mirror all keep working.)

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
- **My change won't submit even at `LLM-Review +1`.** Check for **unresolved comments**
  (the submit rule is `LLM-Review=MAX AND -has:unresolved`) and mark them resolved.

---

Track your work in rebar (see [`CLAUDE.md`](CLAUDE.md) and [`docs/`](docs/)); the Gerrit
server + review-bot architecture is documented in
[`docs/gerrit-aws-setup.md`](docs/gerrit-aws-setup.md).
