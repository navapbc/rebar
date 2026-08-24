# Your first change to rebar

This guide explains how to prepare, submit, and land one small change to rebar. For the complete contributor reference, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Review overview

rebar does not use GitHub pull requests. Code review happens on a self-hosted **Gerrit** server. GitHub is a read-only mirror of `main`. Every change to `main` passes through Gerrit. An LLM code review and CI must both approve a change before it can land. [Review policy](review-policy.md) defines the vote semantics.

The workflow has a one-time setup and a per-change review loop.

## One-time setup

1. **Sign in.** Open <https://rebar.solutions.navateam.com> and select **Sign in with GitHub**. Gerrit creates an account from your GitHub identity.
2. **Generate an HTTP password.** In Gerrit, open **Settings → HTTP Credentials** and generate a password. Git uses this password to authenticate pushes.
3. **Clone the repository.** Use the authenticated `/a/` URL.

   ```bash
   git clone https://<your-user>@rebar.solutions.navateam.com/a/rebar
   cd rebar
   ```

   The clone configures `origin` as the Gerrit remote.
4. **Install the repository hooks.** The supported command installs the commit checks and the Gerrit hook that adds a `Change-Id`. It also verifies the hook configuration.

   ```bash
   make hooks
   ```

If OAuth returns to the login page, clear the site cookies and sign in again. If `make hooks` reports a failure, follow the hook troubleshooting guidance in [CONTRIBUTING.md](../CONTRIBUTING.md#1b-clone-from-gerrit-and-install-the-change-id-hook).

## Your first change (the per-change loop)

1. **Get a ticket id.** Every commit must reference a rebar ticket that resolves in the store. External contributors open a GitHub issue that describes the defect or improvement. A maintainer creates the rebar ticket and provides its identifier. A maintainer may sponsor a contribution by preparing the ticket or helping with the patch. The change still passes through Gerrit before it reaches `main`.
2. **Branch from `main`.**

   ```bash
   git fetch origin && git checkout -b my-first-change origin/main
   ```

3. **Make the edit and commit it.** Use `git commit -s` to add the DCO `Signed-off-by:` line. See [Sign your work](../CONTRIBUTING.md#sign-your-work-dco) for the policy. Add a `rebar-ticket:` trailer with the identifier from the maintainer.

   ```bash
   git commit -s -m "fix: correct the widget count in the summary

   rebar-ticket: <id-from-the-maintainer>"
   ```

   The commit hook adds the `Change-Id:` trailer.
4. **Push for review.** Push to the Gerrit review ref. This operation creates or updates a review and does not update `main`.

   ```bash
   git push origin HEAD:refs/for/main
   ```

   The push output includes a link to the Gerrit change.
5. **Read your votes.** Two bots vote on the change:

   - **A code review finding** requires a correction and another push.
   - **A `coverage-gap` `-1`** identifies an infrastructure condition. A maintainer will investigate it.
   - **A `Verified` `-1`** means CI failed. Open the linked run and identify the cause. Use `recheck` only for a demonstrated environmental fault, such as a runner outage, a missing workflow dispatch, or a transport failure. State the evidence in the recheck comment. A nondeterministic test failure is a defect. Use the [`/rebar-debug` workflow](../examples/agent-skills/rebar-debug/SKILL.md) to reproduce it, establish the root cause, and fix the affected class.

   See [Review policy](review-policy.md) for the complete vote definitions.
6. **Amend and push again.** The `Change-Id` keeps the amended commit attached to the same review. Do not create a new commit for a review correction.

   ```bash
   git commit --amend --no-edit
   git push origin HEAD:refs/for/main
   ```

   Repeat this step until both votes are `+1` and no comments remain unresolved.
7. **A maintainer submits the change.** Gerrit Submit becomes available after both votes reach `+1`, all comments are resolved, and the change is mergeable. Gerrit uses Rebase If Necessary and rebases the change onto the current `main` tip when possible. A maintainer resolves any textual merge conflict that Gerrit cannot resolve. Gerrit then updates `main` and replicates it to GitHub. See [CONTRIBUTING.md](../CONTRIBUTING.md#2e-land-it-yourself-with-submit) and [ADR 0047](adr/0047-retire-autolander-rebase-if-necessary.md) for the landing policy.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `missing Change-Id in message footer` | Run `make hooks`, then run `git commit --amend --no-edit` to add the trailer. |
| A push to `main` was rejected | Push to the review ref with `git push origin HEAD:refs/for/main`. |
| A correction created a second change | Amend the original commit and retain its `Change-Id`. |
| CI rejects a commit without a ticket | Add a `rebar-ticket: <id>` trailer that resolves in the store. Obtain the identifier from the maintainer on the GitHub issue. |
| `Signed-off-by` is missing | Run `git commit -s --amend`. |
| `Verified -1` reports an environmental fault | Confirm the fault from the run evidence, then comment `recheck` with the reason. Do not use `recheck` for a nondeterministic test. |
| OAuth returns to the login page | Clear the site cookies and sign in again. |
| Hook installation fails | Read the `make hooks` output and follow the [hook troubleshooting guidance](../CONTRIBUTING.md#1b-clone-from-gerrit-and-install-the-change-id-hook). |

## See also

- [CONTRIBUTING.md](../CONTRIBUTING.md) provides the complete contributor reference, including accounts, review gates, and submit authorization.
