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
> `recheck` to re-run CI) → **Submit** the change → it replicates to GitHub `main`. `main` is
> **Rebase-If-Necessary**, so Gerrit rebases onto the current tip and submits server-side; a
> textual conflict is the only case you rebase by hand (§2e).

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

### 1_0. Prerequisite — Git ≥ 2.38

**rebar requires Git 2.38 or newer to develop and test.** The two-clone convergence
regressions merge divergent tracker histories with `git merge-tree --write-tree`, added in
Git 2.38. That floor is **declared and enforced, never skipped**: on an older client the
test suite refuses to start and names the required version, because a regression that
quietly does not run reads as coverage while providing none.

Check with `git --version`. Older? macOS: `brew install git` (the Xcode command-line Git
can lag); Debian/Ubuntu: the git-core PPA or a backports build. The floor value is
single-sourced in `.github/git-version-floor.txt`, shared by `tests/conftest.py`, the
`Git version floor gate` CI step, and `tests/unit/test_git_version_floor.py`.

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
# `make hooks` installs it (and the pre-commit gates) idempotently and worktree-safely;
# `make install` runs it for you.
make hooks
```
Git will prompt for your HTTP password on the first authenticated fetch/push; use a
credential helper (`git config --global credential.helper store`/`osxkeychain`) so you
aren't re-prompted.

Set up the local dev env per [`docs/local-dev-env.md`](docs/local-dev-env.md) (run
`make install`, which calls `make hooks` for you). `make hooks` wires **both** gates in one
idempotent step: the check-only **`pre-commit`** hook (lint/format/typecheck) and Gerrit's
**`commit-msg`** Change-Id stamper, the latter via
[`scripts/install-gerrit-hook.sh`](scripts/install-gerrit-hook.sh).

> **Never install the Gerrit hook by downloading it straight onto
> `$(git rev-parse --git-path hooks/commit-msg)` or `.git/hooks/commit-msg`.** Git hooks
> live in the **shared common dir**, so from inside a linked worktree both spellings
> resolve to the *main* checkout's hook file — the slot pre-commit's wrapper occupies. The
> curl clobbers that wrapper for **every worktree on the host**, and nothing complains
> until a Gerrit push is rejected for a missing `Change-Id`, possibly from a different
> worktree (bug 84aa). `scripts/install-gerrit-hook.sh` resolves the common dir
> deliberately, installs into `commit-msg.legacy` when pre-commit owns the slot, and
> refuses loudly rather than overwriting a hook it does not recognise.

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

Follow the [documentation policy](docs/documentation-policy.md) for every new or edited maintained text.

**Check whether you edited a generated file.** Several checked-in files are derived from a source elsewhere in the tree. CI regenerates these files and fails on differences. Each generated file identifies its regeneration command through a banner or a top-level `_generated_by` key. The [Generated artifacts catalog](docs/generated-artifacts.md) lists each file with its source, regeneration command, and enforcement gate. Change the source and run the regeneration command when a generated file needs correction.

**Every commit must reference a rebar ticket.** CI's `Verified` gate rejects a commit to
`main` whose message does not reference a rebar ticket that resolves in the store — via a
`rebar-ticket: <id>` trailer (preferred) or a leading `<id>:` subject line. `<id>` may be an
alias, full id, short id, or Jira key. See
[docs/commit-ticket-trailer.md](docs/commit-ticket-trailer.md).

```bash
git commit -m "component: what changed and why

rebar-ticket: blank-guild-koi"
```

**Wrap the message body at ~72 columns** (and keep the subject under ~50). Gerrit's
commit-message validator warns otherwise — `warning: subject >50 characters` and
`warning: too many message lines longer than 72 characters; manually wrap lines` — and
Gerrit renders the message as preformatted text, so the newlines you write are the
newlines reviewers see. Nothing in rebar reflows a commit message: the wrapping in a
Gerrit change description is exactly what the author committed (ticket
`gargantuan-illhumored-drongo`). Wrap prose at the column; leave code blocks, trailers,
and URLs unwrapped even when they exceed it.

This is **enforced locally at commit time** by the `commit-message-wrap` hook
(`scripts/check_commit_message_wrap.py`), installed by `make install` / `make hooks`.
Like the other gates it is check-only — it never rewrites your message, it names the
lines to wrap:

```
commit message does not follow the 50/72 rule:
  - line 3 is 222 chars (limit 72): 'Eval fixtures now emit one Acceptance Criteria…'
```

Exempt, because wrapping them would break them: trailers (`rebar-ticket:`,
`Change-Id:`, `Signed-off-by:`), `TAG=` lines, fenced code blocks, indented literal
blocks, table rows, and any line whose length comes from a single unsplittable token
(a URL, path, or long identifier). Comment lines and the `git commit --verbose` diff
are ignored.

Two notes on the hook:

- It runs at git's **`commit-msg`** stage, not `pre-commit` — the message does not exist
  yet when `pre-commit` hooks run. `default_install_hook_types` in
  `.pre-commit-config.yaml` installs both stages.
- Gerrit's own `commit-msg` hook (the one that stamps `Change-Id`) shares that slot.
  pre-commit installs in **migration mode**, preserving it as `commit-msg.legacy` and
  running both, and `make hooks` verifies the `Change-Id` chain survived. **Never run
  `pre-commit install -f`** here: it would drop Gerrit's hook and every push would be
  rejected for a missing `Change-Id`.
- It is a local guardrail, bypassable with `git commit --no-verify`, not an enforcement
  boundary — CI does not check wrapping.

**Comments and docstrings must not anchor on rot-prone history tokens.** The ticket
system owns history; source comments carry *current state*. CI's comment-hygiene gate
(`scripts/check_comment_hygiene.py`, wired into `_build-and-test.yml`) fails the build
when a comment or docstring cites a bare commit SHA (`cb858e468d`), a CI run/job id
(`run 30721408463`), or a dated incident narrative (`the 2026-07-30 losses`) — all of
which go stale or unresolvable as history is rewritten, runs expire, and dates lose
context. Write one of the three durable forms instead:

- **Cite a resolvable ticket** — a grouped hex id (`5200-e04e-246e-4aae`) or word-triple
  alias (`robe-creek-zealot`); ADR references (`ADR 0025`) also count. `rebar show <id>`
  must resolve it.
- **Drop the token, keep the prose** — describe the behavior or decision in words that
  stay true without the reference (e.g. keep "PR #375 review", drop raw thread ids).
- **Vendor-pinned external references** — mark the line with `context: external` when a
  token genuinely lives outside rebar's ticket system (an upstream issue id, a pinned
  vendor SHA) and cannot be replaced.

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

**Documentation-only routing.** A patchset whose every changed path is on the narrow
documentation allowlist runs one Ubuntu lane containing the same ADR number/index/link,
docs-index/dead-link, README quickstart, CLI/MCP reference, and environment-registry gates
used by the full matrix. The path decision is made by classifier code and its complete
allowlist sparse-checked out from trusted `origin/main`, never by code from the patchset.
Python, scripts, workflows/actions, dependencies and locks, tests/fixtures, configuration
or policy, unrecognized paths, and an empty/malformed/unresolvable `HEAD^..HEAD` diff all
select full Verify. A classifier failure also starts the full fallback route and still
blocks `Verified`, so routing cannot fail open.

To extend the allowlist, first prove the new path class cannot change executed build,
test, packaging, policy, or CI behavior; add positive and conservative negative classifier
tests; and land the classifier change through full Verify. Do not put the allowlist in a
patchset-controlled config file or duplicate documentation commands in either route: the
single definition is `.github/actions/docs-gates/action.yml`.

The two votes are **independent**: an LLM finding does not fail CI, and a CI flake does not
change the review verdict. **Only the two bots and administrators may cast either label**,
so you cannot self-approve or self-verify your own change. (Both labels block submit today —
the `Verified` requirement was activated 2026-07-02; see the status note above.)

**Reading a `-1` (quick version).** An `LLM-Review` `-1` is either a **finding** in
your code (`[LLM-Review: BLOCK — finding]`, with inline comments → fix, amend, re-push,
§2d) or a **coverage-gap** infra veto (`[LLM-Review: BLOCK — coverage-gap (…)]` → the
review couldn't fully run; **not your code** — once infra recovers, comment
**`rerun-llm-review`** on the change to have the bot re-review it yourself, don't "fix"
your diff; the bot refuses the trigger only when the `-1` is a real finding). A
`Verified` `-1` means CI failed: open the linked run, fix a real failure and re-push
(§2d), or comment **`recheck`** to re-run CI on the same patchset for a flake. (The two
triggers are parallel but deliberately share **no substring**: `recheck` re-runs CI,
`rerun-llm-review` re-runs the LLM review. CI's matcher is substring-based and lives in a
tool we do not own, so an LLM trigger word containing `recheck` would fire CI as well —
which is why the older word was retired rather than kept as an alias.)

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

#### Hold a change with known issues in **Work In Progress**

If a pushed change has known unresolved issues — findings you are still folding in, a
defect you spotted after pushing, or any deliberate hold — mark it **WIP immediately**.
Gerrit will not let a WIP change be submitted, so this is the hold itself, not a note
about one:

```bash
# at push time, if you already know it isn't ready
git push gerrit HEAD:refs/for/main%wip

# on a change that is already up (n = the change number)
curl -u "$USER:$PASS" -X POST https://rebar.solutions.navateam.com/a/changes/<n>/wip
# …and when it's resolved:
curl -u "$USER:$PASS" -X POST https://rebar.solutions.navateam.com/a/changes/<n>/ready
```

`%ready` on the next push does the same as the `/ready` call. The Gerrit UI's **Mark as
work in progress** / **Mark as ready** buttons are equivalent.

**Why the ticket comment isn't enough.** A hold recorded in a ticket, a session log, or a
Gerrit comment is *advisory*: it relies on whoever submits next having read it. WIP is
*enforceable* — it is the only hold every submitter, human or bot or another agent
session, is structurally forced to respect. Change 1727 is the worked example: five known
defects were recorded on its ticket and a peer session, correctly following the standing
"both votes green ⇒ Submit" rule (§2e), submitted it anyway. Both votes being green is a
statement about the gates, never about whether *you* think the change is done.

### 2e. Submit
**Land with a plain Gerrit Submit once both votes are green.** Once your change has
**`LLM-Review +1` AND `Verified +1`** and no unresolved comments, land it yourself with a plain
Gerrit **Submit** — the Submit button on the change page, or `ssh -p 29418
<you>@rebar.solutions.navateam.com gerrit review --submit <change>,<patchset>`, or
`POST /a/changes/<n>/submit`. `main` is **Rebase-If-Necessary** (ADR-0047): Gerrit fast-forwards
when it can and otherwise **rebases your change onto the current `main` tip and submits it
server-side, atomically, under its own lock** — so you do **not** pre-rebase for the ordinary
(non-conflicting) case. Gerrit then merges and **replicates the new `main` to GitHub** (that
replication is the only way GitHub `main` advances). The two votes still gate (§2c); with them
green and the change mergeable, Submit lands it. There is no landing bot, queue, or hand-off
label — you run Submit yourself.

> **A textual conflict is the only time you rebase by hand.** Under Rebase-If-Necessary, Gerrit
> does the rebase for you at submit time — the old requirement that your change sit directly on
> the current `main` tip is gone. The one exception is a **textual merge conflict** Gerrit
> cannot resolve: it refuses the submit and hands the change back. Then, and only then, do the
> ordinary `git fetch origin && git rebase origin/main` (a **feature-branch** merge change
> re-merges: `git merge --no-ff gerrit/feature/<name>` onto the new `main`), resolve the
> conflict, re-push `HEAD:refs/for/main`, wait for a fresh `Verified`, and **Submit** again.
>
> **Safety net — post-merge `main` CI (ADR-0047).** A clean (non-conflicted) rebase is submitted
> without re-running integration CI, so the detector for a rare *semantic* conflict between two
> individually-green changes is the existing **CI that runs on every push to `main`** (via the
> GitHub mirror). On the rare red `main`, a human does a **manual revert** through Gerrit —
> there is no auto-revert. Runbook: [`infra/runbooks/main-red-post-merge.md`](infra/runbooks/main-red-post-merge.md).

> **Submitting requires contributor authorization.** The **Submit** action is restricted to
> the `Contributors` group (plus Administrators) — anyone may push to `refs/for/*` to
> *propose* a change, but only an authorized contributor (or an admin) can *land* one, even
> when both votes are `+1`. If Submit is unavailable to you, ask an admin to add your Gerrit
> account to the `Contributors` group (or to submit on your behalf). This is enforced
> natively by the Submit ACL in `infra/gerrit/project.config` (managed by
> `infra/gerrit/setup-project.sh` via `CONTRIBUTOR_MEMBERS`).

### 2f. Close AFTER the change merges — then satisfy the close precheck

Do not close a ticket until its change is `Verified +1` and has **merged** — that ordering is
the review-gate policy above, not something the close gate enforces. The close's completion
precheck is narrower: when the ticket records `file_impact` (and the completion-verification
close gate is on), it requires a commit reachable from **your worktree's current HEAD** carrying
a `rebar-ticket:` trailer (or a leading `<id>:` subject) for the ticket **or any transitive
descendant**; merge status is not checked, so a local unlanded commit satisfies it. If your
worktree's history lacks such a commit (a fresh worktree, or a checkout that never had it), the
close fails with "cannot confirm the work landed" — fast-forward first: from inside the worktree
run `git fetch origin && git merge --ff-only origin/main` (or `git pull --ff-only`). To close a
stacked story against its own commit while the worktree stays at the epic tip, use
`rebar transition <id> in_progress closed --ref <sha>` — `--ref` retargets the completion
verification and signature to that ref's tree; it does **not** narrow the referencing-commit
scan — see `docs/plan-review-gate.md` §"Which commit the completion gate verifies — `--ref`".

> **NEVER** fast-forward, reset, or stash the shared main checkout to satisfy a close — it may
> hold uncommitted operator WIP that a fast-forward would clobber. Advance **your worktree**, or
> spin up a throwaway worktree at `origin/main`, instead.

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
reached at — in short, sign off with your own real, configured git identity. This is
**enforced at push time**: Gerrit rejects an unsigned push to
`refs/for/*`, so a commit without a `Signed-off-by:` trailer cannot even reach review.
Contributor-facing guidance (`AGENTS.md`, `.agents/rules/*.md`, this file, `docs/`) must
describe this same identity-neutral contract — `make lint` runs
`scripts/check_dco_identity.py`, which fails the build if a personal sign-off identity is
hardcoded back into that guidance. A dedicated automation identity (e.g. a bot account) is
scoped to its own machine-local config and to automation-owned paths (`infra/`,
`.github/workflows/`, `tests/`), which the check excludes.

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

### 3_0. Mirror `main` CI runs on a SCHEDULE, not on every push

The mirror runs the test suite, the optionality check, and the authorship gate against `main`
on a **6-hourly schedule** rather than on every push. Gerrit's `Verified` vote already tests
every patchset before it lands, so the mirror's job is to answer *"is `main` healthy now?"* —
not to re-issue a per-commit verdict.

It used to run per-push, and that actively misled: those lanes cancel an in-progress run when
a new commit arrives, GitHub renders a cancelled run as a **red X**, and a healthy `main`
therefore grew a trail of red X's. GitHub offers no way to suppress that, so the trigger moved
instead of the symptom.

What this means in practice:

- **Nothing about landing a change is different.** The pre-merge gate is untouched: every
  change still needs `LLM-Review +1` **and** `Verified +1`, and the Verified lane runs the same
  shared reusables against your exact patchset.
- **To check `main` on demand**, dispatch the workflow (Actions → *Test Suite (mirror)* → Run
  workflow) instead of waiting for the tick. Leave `run_external` unchecked unless you
  deliberately want the live, billable external tier.
- **If a scheduled run is red**, its run summary already carries the last-known-green `main`
  SHA and a copy-pasteable `git bisect run` recipe — a red tick covers every commit since the
  last green one, so start there rather than reconstructing the technique by hand.

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
`Verified`) against the feature branch. Land each story once both are `+1` and nothing is
unresolved — with a plain Gerrit **Submit**, exactly as in §2e; it merges into
`feature/<name>`, not `main`.

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
make hooks   # idempotent + worktree-safe; see §1b for why not a bare curl
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
against the merge tree. Land it with a plain Gerrit **Submit** once both votes are green (§2e)
— Gerrit lands the whole feature on `main` atomically and replicates it to the GitHub mirror.
(When `main` advances under the open merge change before you Submit, refresh it with the
ADR-0025 re-merge below.)

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
- **Both votes are `+1` but the change still won't land.** Under Rebase-If-Necessary
  (ADR-0047), both-votes-green + mergeable = submittable, so first confirm there are **no
  unresolved comments** and that you have submit authorization (§2e). If Submit reports a
  **textual merge conflict**, Gerrit couldn't rebase it for you — `git fetch origin && git
  rebase origin/main`, resolve, re-push, wait for a fresh `Verified`, then Submit again (§2e).
- **CI (`Verified`) didn't run / no run appeared.** The CI dispatch (Gerrit →
  gerrit-to-platform → GitHub Actions) may be down. Comment **`recheck`** to re-trigger; if
  still nothing, it's an infra issue for maintainers (see
  `infra/runbooks/two-vote-gate-rollback.md`) — not a problem with your diff.
- **`Verified -1` and you think the failure is not your diff.** Split the two cases before
  you do anything — they have opposite remedies.
  - **A provably environmental fault** (a TLS/promisor cert error, a dispatch that never
    arrived, a runner outage): comment **`recheck`** to re-run CI on the same patchset (§2c),
    and **say in the comment why you believe it is environmental**. A new patchset also
    re-runs CI and cancels the stale run.
  - **A nondeterministic test** (it passes on a re-run with no change, or only fails in
    certain orderings): that is a **bug**, and `recheck` is not the remedy. Root-cause it —
    reproduce deterministically via the `/rebar-debug` workflow and fix the class — then
    re-run CI. Retrying a flake until it goes green slows development, wastes tokens, and
    erodes CI as a regression oracle. This rule is stated in
    [AGENTS.md](AGENTS.md) §"Git workflow"; the standing example is the order-dependent
    leaked-logger flake, which cost four `Verified -1`s and several recheck cycles before one
    root-cause pass fixed it class-wide.
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
  lives once in the shared preamble (`SHARED_STANCE_PREAMBLE` in
  `src/rebar/llm/prompting/prompts.py`), prepended to every pass system prompt by
  `_resolve_system` and embedded in the verifier templates via their `{{shared_prefix}}`
  variable (`shared_plan_prefix`) — don't re-derive it per prompt. The preamble/prefix is
  single-sourced there; never re-inline it in a template, and never re-introduce a `{{plan}}`
  variable or a `<!--volatile-->` marker in the verifier templates
  (`tests/unit/workflow/test_shared_prefix.py` is the canary).

This is enforced deterministically by `tests/unit/test_reviewer_prompt_hygiene.py`
(`test_no_bare_do_not_only_blocks`), which runs in CI — a re-runnable guard, not a hand checklist.

### 7a. Criterion ids cited in the prose guides are PINNED

The hand-written author guides under `src/rebar/_guides/` (`rebar explain plan` / `review` /
`commit-trailer`) cite criterion ids inline — "name the alternative you rejected (G6)" — but the
criterion's rubric in the registry, not the guide sentence, is authoritative. Bug `828a` shipped a
month of contradiction between the two because nothing coupled them. A gate cannot judge whether
prose faithfully paraphrases a rubric, so instead it refuses to let a cited criterion change
**silently**: `src/rebar/_guides/criterion-pins.json` pins a digest of every cited criterion's
authoritative text, and `python -m rebar.llm.plan_review.registry validate-routing` (CI) fails when
a pin no longer matches. Nothing regenerates the prose — only the derived manifest.

- **Editing a criterion** (its rubric under `src/rebar/llm/reviewers/plan_review_*.md`, or its
  routing/checklist): CI reports the cited guide as `stale`. Re-read the criterion
  (`rebar explain <id>`), confirm every guide sentence citing it still holds — **fix the prose if
  it doesn't** — then re-pin with
  `python -m rebar.llm.plan_review.guide_parity regenerate` and commit the manifest.
- **Editing a guide** (adding or dropping a citation): CI reports `unpinned` or `orphan`; run the
  same regenerator and commit. A pinned id that has left the registry is reported as `retired` —
  there the **guide** is what must change, since it cites a criterion that no longer exists.

---

Track your work in rebar (see [`AGENTS.md`](AGENTS.md) and [`docs/`](docs/)); the Gerrit
server + review-bot architecture is documented in
[`docs/gerrit-aws-setup.md`](docs/gerrit-aws-setup.md).
