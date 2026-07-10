# ADR 0038 — Governance artifacts: lead-maintainer language, CoC version, CODEOWNERS non-goal

**Status:** Accepted (epic breaded-ammonitic-elephant — OSS v1.0.0 front door)
**Date:** 2026-07-09

## Context

Publishing `GOVERNANCE.md` and `MAINTAINERS.md` for the 1.0 community front door
forced three design decisions that are not obvious from the templates (curl's
`GOVERNANCE.md`, the CNCF small-project template, opensource.guide). This ADR
records them so the reasoning survives independently of the tickets.

## Decision 1 — "Lead maintainer" language, not "BDFL"

rebar has a single person with final say, which is structurally a BDFL. We
deliberately use **"lead maintainer"** rather than "BDFL" in `GOVERNANCE.md`.

- **Rationale.** "Lead maintainer" describes the same authority (final say when
  consensus fails) in **role** terms rather than **person** terms, which makes
  succession and role-sharing expressible without rewriting the social contract —
  this is curl's framing. "BDFL" bakes the individual into the title and reads as
  a permanent, personal claim; it also carries baggage some contributors find
  off-putting. The role language costs nothing and ages better.

## Decision 2 — Stay on Contributor Covenant 2.1 for now (3.0 assessed, deferred)

Contributor Covenant **3.0** is the 2026 default (Django, Mastodon, Hanami have
adopted it). We **assessed 3.0 and chose to stay on 2.1** for this change.

- **Rationale.** The load-bearing fix in this story is the **enforcement contact**
  (role-based, with a maintainer-concerning-report escape and a Nava backstop),
  not the CoC body text. 2.1 is stable, widely recognized, and already in the
  repo; 3.0 restructures the enforcement-ladder language and would add a sizable,
  independent review surface that is orthogonal to the contact fix. Bundling a
  full 2.1→3.0 rewrite into the governance story would dilute its review.
- **Revisit trigger.** Adopt 3.0 as a standalone change when we next touch the CoC
  body, or sooner if a contributor requests it. The upgrade is mechanical (swap the
  body, re-apply our contact block) and does not depend on anything here.

## Decision 3 — No CODEOWNERS (GitHub or Gerrit code-owners plugin)

We deliberately **do not** add a `CODEOWNERS` file, nor enable Gerrit's
code-owners plugin.

- **Rationale (tense-correct).** GitHub `CODEOWNERS` only auto-requests reviewers
  and gates merges **on pull requests**. PRs cannot merge here today — a repository
  ruleset rejects PR merges, and story `nonsecular-arthritic-sidewinder` adds an
  auto-answer bot on top — so a `CODEOWNERS` file would be **dead config that
  falsely implies PR review exists**. Gerrit's code-owners plugin is likewise
  skipped: one human owns everything, so per-path owners add ceremony with no
  routing benefit.
- **Revisit trigger.** Reconsider if PR-based merges are ever enabled, or once
  there are enough maintainers that per-path review routing becomes useful.

## Consequences

- `GOVERNANCE.md` §1–§2 use role language; the title "lead maintainer" is defined
  once and reused (including the CoC escape hatch and `MAINTAINERS.md`).
- `CODE_OF_CONDUCT.md` stays on Contributor Covenant 2.1; only the enforcement
  block changed. A future 3.0 bump is a clean, isolated change.
- No `CODEOWNERS` file exists; the absence is intentional and documented here so a
  future contributor does not "helpfully" add one.
