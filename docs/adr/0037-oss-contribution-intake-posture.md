# ADR 0037 — GitHub PR posture: keep PRs enabled + welcome-bot redirect

**Status:** Accepted (epic breaded-ammonitic-elephant — OSS v1.0.0 front door)
**Date:** 2026-07-09

## Context

`main` advances only through Gerrit review; GitHub is a read-only mirror and a
repository ruleset rejects PR merges. So a pull request opened against the mirror
**can never merge**. The question is what happens to it. Leaving PRs open and
unanswered is the posture universally condemned across peers (Gerrit's own mirror
with rotting PRs; pre-archive AOSP). We want the opposite: convert a drive-by PR
into a repeat contributor.

## Decision

**Keep GitHub pull requests ENABLED, and auto-answer them with a warm redirect
bot that closes (does not lock) the PR.** The bot (`dessant/repo-lockdown`,
SHA-pinned, in `.github/workflows/lockdown.yml`) thanks the author, explains the
mirror, and deep-links Gerrit sign-in + the `docs/your-first-change.md` tutorial,
and offers the shepherded-patch fallback. The thread stays open for discussion
(LibreOffice/Wikimedia pattern). Issues are real intake and are never touched by
the bot.

## Rejected alternatives

### Native disable (`has_pull_requests: false`) — rejected (E1)
GitHub's Feb-2026 setting can turn PRs off entirely (Chromium/Qt/torvalds use it).
**E1 finding:** the field exists and is settable on `navapbc/rebar`, but the
fork-side UX is a **silent dead-end** — the "Contribute / New pull request"
affordance disappears with **no redirect to Gerrit**. That is zero-cost but
converts nobody; a would-be contributor just bounces off. We prefer an enabled PR
surface that a bot turns into a funnel.

### GerritBot-style automatic PR → Gerrit import — rejected (non-goal)
Automatically importing GitHub PRs into Gerrit changes (the `gerrit
github-pullrequest` plugin) is a non-goal. Only Go operates one at scale, the
plugin is effectively dormant, and even at Go's scale bridged PRs merge at ~34% vs
~78% for native changes (golang discussion #61182). The complexity is not worth it
for a solo-maintained project; a human shepherd converting the worthwhile patches
is both simpler and higher-signal.

## Revisit trigger

Revisit if **PR volume exceeds solo-shepherd capacity** — i.e., if converting the
worthwhile PRs by hand becomes a bottleneck. At that point re-evaluate the
GerritBot import (accepting its lower merge rate for throughput) or a triage
rotation.

## Consequences

- `.github/workflows/lockdown.yml` closes fork PRs with the redirect message;
  `dependabot[bot]` is exempt (its PRs are advisory and a maintainer converts them).
- The shepherded-patch path is documented in `CONTRIBUTING.md`. Because Gerrit
  grants `forgeAuthor` to Registered Users (inherited from All-Projects; E2), a
  shepherd's `git am` push **preserves the original contributor as commit author**.
- Maintainer-opened test PRs are intentionally **not** exempt — a closed test PR is
  the verification that the bot works.
