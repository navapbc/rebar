<!--
  ⚠️  IMPORTANT: GitHub pull requests are NOT merged for this project.
  GitHub is a read-only mirror; `main` only advances through our Gerrit review
  flow and then replicates out to GitHub.
-->

## 🚦 GitHub PRs are not merged here — please use Gerrit

Thanks for your interest in contributing to **rebar**! This repository is a
**read-only mirror**. `main` advances only via changes that pass two independent
gates on our self-hosted **Gerrit** server (an `LLM-Review` vote and a `Verified`
CI vote), and Gerrit then replicates the new `main` to GitHub. **Pull requests
opened here cannot be merged** and a repository ruleset rejects direct pushes and
PR merges to GitHub `main`.

### How to land your change

New here? The friendly walkthrough is
**[docs/your-first-change.md](../docs/your-first-change.md)**; the full reference is
**[CONTRIBUTING.md](../CONTRIBUTING.md)**. The short version:

1. Sign in to `https://rebar.solutions.navateam.com` (GitHub OAuth), generate an
   HTTP password, and clone from Gerrit.
2. Install the `commit-msg` hook so your commit carries a `Change-Id`.
3. Reference a rebar ticket in your commit message (a `rebar-ticket: <id>`
   trailer, or a leading `<id>:` subject) — CI's `Verified` gate requires it.
4. Push for review: `git push origin HEAD:refs/for/main`.
5. Address the `LLM-Review` and `Verified` bot votes; amend and re-push until
   both are `+1`, then submit.

If you opened this PR to share a patch or start a discussion, that's welcome —
but the change itself needs to go through Gerrit to land. See
[SUPPORT.md](../SUPPORT.md) for other channels.
