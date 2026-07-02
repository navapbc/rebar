# ADR 0021 — Replicate `refs/changes/*` to GitHub for CI (scoped broadening of ADR-0010)

**Status:** Accepted (epic 1fa8 / story S2). Amends **ADR-0010** (Gerrit→GitHub replication).
**Date:** 2026-07-01

## Context

ADR-0010 replicates only `refs/heads/main` + `refs/tags/*` one-way to GitHub (published
history), deliberately keeping change refs and meta refs off the mirror. Epic 1fa8 runs
CI on GitHub Actions via `gerrit-to-platform`: the workflow must check out the exact
patchset under review, but a Gerrit change lives at `refs/changes/NN/NNNN/P` — which,
under ADR-0010, never reaches GitHub. `gerrit-to-platform`'s production pattern
(ONAP/O-RAN-SC) mirrors change refs so `checkout-gerrit-change-action` can fetch them
from GitHub.

## Decision

1. **Add one scoped push refspec** to `infra/gerrit/replication.config`:
   `push = refs/changes/*:refs/changes/*` (non-force, no leading `+`, `mirror = false`).
   GitHub now carries the published branch, tags, AND per-patchset change refs.

2. **Scoped to `refs/changes/*`, NOT wildcard `refs/*`.** A wildcard would also
   replicate `refs/meta/config`, whose content embeds the inbound-webhook token
   (ADR-0014). `replicatePermissions = false` stays false; `refs/meta/*` is never
   pushed. This is the security-critical constraint of the broadening.

3. **Replication lag is tolerated by a fallback, not a hard dependency.**
   `checkout-gerrit-change-action` fetches `GERRIT_REFSPEC` from the GitHub mirror
   first and falls back to fetching directly from the Gerrit server (`gerrit-url`) if
   the ref hasn't replicated yet. A persistently failing replication is alarmed the
   same way as ADR-0010's main/tags pushes.

## Consequences

- Unmerged/abandoned patchsets become visible on the public GitHub mirror as
  `refs/changes/*` (they are already public in the public Gerrit project, so no new
  disclosure). These refs accumulate; GitHub GC and Gerrit's own change lifecycle
  bound them. `mirror = false` means abandoned-change ref deletions are not pruned on
  GitHub — acceptable for a PoC; revisit if the mirror grows unwieldy.
- The one-way-door contract (Gerrit sole writer, non-force, no `refs/meta/*`) is
  preserved; only the *set of replicated data refs* widened. ADR-0010 otherwise stands.
- ADR-0014's "webhook token never replicated" property is explicitly preserved by the
  scoped refspec + `replicatePermissions = false`.

## Alternatives considered

- **Wildcard `refs/*`** (as some g2p docs suggest): rejected — leaks `refs/meta/config`.
- **No replication; always fetch from Gerrit** via the action's `gerrit-url` fallback:
  workable but diverges from the g2p production pattern and puts every CI checkout on
  the Gerrit box's bandwidth; mirroring is the standard and keeps the box out of the hot
  path, with the fallback only for lag.
