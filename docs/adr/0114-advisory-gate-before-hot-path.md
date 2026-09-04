# ADR 0114 — New required CI gates prove green advisory before entering the Verified hot path

**Status:** Accepted
**Date:** 2026-09-04

## Context

Incident `90c7-c465-4037-42d5` exposed a self-referential lock in the Gerrit landing path. The
new `scanner-integration` check was wired directly into the required `Verified` vote aggregate in
`.github/workflows/gerrit-verify.yaml`. Because it failed on every full-route change, including
changes that would repair or revert the check, the fleet could not earn `Verified +1`; only a
human force-submit broke the lock.

The failure mode is specific to checks that enter the required vote path before the project has
evidence that the check can pass on `main` independently of that path. A broken non-voting job is
visible but does not prevent the fix from landing. A broken job inside the aggregate prevents the
fix from collecting the very vote required to land.

ADR 0110 already applies the same shape of restraint to change-shape enforcement: new signal first
enters as advisory, not as a blocking floor. Required CI gates need the same staging discipline
when they would become part of the `Verified` hot path.

## Decision

Before any new check is added to, or promoted into, the required `Verified` vote aggregate in
`.github/workflows/gerrit-verify.yaml`, it MUST first run as an advisory job: non-voting,
non-blocking, and outside the aggregate's required `needs` and routing conditions.

That advisory job MUST show at least one passing CI run on `main` independent of the required
`Verified` hot path before a later, separate change may promote it into the aggregate. The
promotion change may then add the job to the aggregate's blocking `needs`, route conditions, or
other required-vote wiring.

This policy applies only to `.github/workflows/**` changes that add or promote a required gate into
the `Verified` vote aggregate. It does not apply to ordinary test-suite additions, such as adding
new tests under `tests/` that are exercised by an existing required suite.

## Consequences

- A new required CI gate is a two-step rollout: first advisory with an independent passing `main`
  run, then a later promotion into the `Verified` hot path.
- A defective new gate remains observable while advisory, but it cannot block its own repair or
  revert from landing.
- Promotion reviews have a concrete evidence requirement: identify the prior advisory green run on
  `main` before accepting the hot-path wiring change.
- Ordinary tests continue to enter through the existing test suites; this ADR does not require a
  separate advisory period for each new test file or scenario.
- The policy adds process latency for genuinely new required gates, but the cost is deliberate and
  smaller than recovering from another fleet-wide self-lock.
