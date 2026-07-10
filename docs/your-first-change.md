# Your first change to rebar

Welcome! This is a friendly, start-to-finish walkthrough of getting **one small
change** reviewed and landed in rebar. It is deliberately narrow: do this once and
the workflow will feel natural. For the complete reference — every flag, every edge
case — see [CONTRIBUTING.md](../CONTRIBUTING.md). This page is the on-ramp;
CONTRIBUTING.md is the map.

## How review works here (30 seconds)

rebar does **not** use GitHub pull requests. Code review happens on a self-hosted
**Gerrit** server, and GitHub is a **read-only mirror** of `main`. Every change is
gated by **two bot votes** before it can land — an LLM code review and CI. You don't
need to understand the vote details yet; when you get a vote, the short version below
tells you what to do, and [docs/review-policy.md](review-policy.md) has the full
semantics.

The flow has two layers, borrowed from every Gerrit project's onboarding: a
**one-time setup** you do once, and a **per-change loop** you repeat for every change.

## One-time setup (~5 minutes)

You only do this the first time.

1. **Sign in.** Go to <https://rebar.solutions.navateam.com> and sign in with the
   **"Sign in with GitHub"** (OAuth) button. This creates your Gerrit account from
   your GitHub identity — no separate signup.
2. **Generate an HTTP password.** In Gerrit, open **Settings → HTTP Credentials**
   and generate a password. This is what git uses to authenticate your pushes.
3. **Clone the repo** using the authenticated `/a/` URL (note the `/a/`, which means
   "authenticated"):
   ```bash
   git clone https://<your-user>@rebar.solutions.navateam.com/a/rebar
   cd rebar
   ```
   Your clone's `origin` remote now points at Gerrit — that's exactly what you want.
4. **Install the commit-msg hook.** Gerrit tracks each change by a `Change-Id`
   trailer, which this hook adds automatically so amended commits stay linked to the
   same review:
   ```bash
   curl -Lo .git/hooks/commit-msg https://rebar.solutions.navateam.com/tools/hooks/commit-msg
   chmod +x .git/hooks/commit-msg
   ```

> **Two common setup snags.** *OAuth loop* — if signing in bounces you back to the
> login page, clear the site's cookies and retry; it's a stale session, not a
> rejected account. *Hook curl fails* — if the `curl` above writes an empty or HTML
> file, you're likely not signed in in that browser session; open the URL once in a
> browser first, then re-run `curl`.

## Your first change (the per-change loop)

1. **Get a ticket id.** Every commit must reference a rebar ticket that resolves in
   the store. As an outside contributor you don't create tickets directly: **open a
   GitHub issue** describing what you want to change (the bug or the improvement). A
   maintainer files the rebar ticket and replies on the issue with the **ticket id**
   to use. (For a truly tiny fix, ask on the issue — a maintainer may shepherd the
   patch in directly, which skips Gerrit entirely; see CONTRIBUTING.md.)
2. **Branch from `main`.**
   ```bash
   git fetch origin && git checkout -b my-first-change origin/main
   ```
3. **Make your edit, then commit with a sign-off and the ticket trailer.** Use
   `git commit -s`, which adds the DCO `Signed-off-by:` line certifying you wrote the
   change (see <https://developercertificate.org>). Add a `rebar-ticket:` trailer with
   the id the maintainer gave you:
   ```bash
   git commit -s -m "fix: correct the widget count in the summary

   rebar-ticket: <id-from-the-maintainer>"
   ```
   The commit-msg hook adds the `Change-Id:` automatically — you'll see it appear in
   the message.
4. **Push for review** to Gerrit's magic ref. This does **not** touch `main`; it
   creates (or updates) a review:
   ```bash
   git push origin HEAD:refs/for/main
   ```
   The `refs/for/main` target is Gerrit's "please review this for main" address. The
   push prints a link to your new change — open it.
5. **Read your votes.** Two bots vote on the change:
   - **A finding** on the code review → fix it, then re-push (next step).
   - **A "coverage-gap" −1** → that's an infrastructure signal, **not** a problem with
     your code; a maintainer will sort it out.
   - **A `Verified` −1** → CI failed. Open the linked run; if it looks like a flake,
     comment **`recheck`** to re-run CI. If it's a real failure, fix and re-push.

     For what each vote means in full, read [docs/review-policy.md](review-policy.md)
     — it's the single source for vote semantics, so this tutorial won't restate the
     details here.
6. **Amend and re-push** to update the same change (thanks to the `Change-Id`, this
   is an update, not a new review — never make a brand-new commit for a fix):
   ```bash
   git commit --amend --no-edit      # or --amend to also edit the message
   git push origin HEAD:refs/for/main
   ```
   Repeat until both votes are green.
7. **A maintainer submits it.** Once both votes are `+1`, a maintainer presses
   **Submit** and Gerrit merges the change and replicates the new `main` out to
   GitHub. As a first-time contributor you won't see (and don't need) a Submit button
   yourself — submit rights come once you've been added as a maintainer. Your job is
   done when both votes are green. 🎉

## Gotchas

| Symptom | Fix |
| --- | --- |
| `missing Change-Id in message footer` | Install the `commit-msg` hook (setup step 4), then `git commit --amend --no-edit` to re-stamp. |
| Your push went to `main` and was rejected | You pushed a branch, not the review ref. Use `git push origin HEAD:refs/for/main`. |
| A fix created a **second** change | You committed anew instead of amending. Squash back to one commit and `--amend`. |
| CI rejects the commit for no ticket | Add a `rebar-ticket: <id>` trailer that resolves in the store (get the id from the maintainer on your GitHub issue). |
| `Signed-off-by` missing | Re-commit with `git commit -s --amend`. |
| `Verified −1` but the code is fine | Likely a CI flake — comment `recheck` on the change to re-run it. |
| OAuth loop / hook curl fails at setup | See the "two common setup snags" note above. |

## See also

- **[CONTRIBUTING.md](../CONTRIBUTING.md)** — the complete contributor reference
  (accounts, the two-vote gate in full, submit authorization, and more).
