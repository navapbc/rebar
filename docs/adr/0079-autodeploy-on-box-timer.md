# ADR 0079 — Continuous auto-deploy: on-box systemd timer, copy-based build context, v1 = review-bot only

> **Renumbered by story 0743:** previously ADR 0026 (a number shared by 2 ADRs — a collision); reassigned to 0079 to make ADR numbers unique. See [RENUMBERING.md](RENUMBERING.md).

**Status:** Accepted (epic 88ab / story 8903 — gall-plume-adder)
**Date:** 2026-07-03
**Relates to:** ADR 0020 (two-vote CI gate), ADR 0021 (replication change refs), ADR 0022 (g2p in container)

## Context

Landing a change on `main` did NOT update the running Gerrit box: the review-bot container's
code, `replication.config`, and g2p config required a MANUAL deploy (and sometimes a restart).
This drifted and toiled — S2's merge-review code landed on `main` while the running bot stayed
pre-S2 (409s on merge changes), and the S3 replication fix was hand-applied. The environment
must AUTO-REFLECT `main` without ever destabilising the LIVE, FAIL-CLOSED gate (no `LLM-Review`
vote ⇒ no submit; a bad deploy could freeze all submissions).

## Decision

### 1. Trigger — on-box systemd timer, NOT GitHub Actions → SSM

`rebar-autodeploy.timer` (2-min) → `.service` oneshot runs `infra/scripts/autodeploy.sh`,
polling the PUBLIC GitHub mirror read-only. Chosen over a GH-Actions→SSM `deploy.yml` because
it adds **no GitHub→AWS trust surface** (no OIDC / `ssm:SendCommand` grant that would make the
GH runner a lateral-movement path). The box already reads the public mirror read-only and holds
an instance role. Only the mirror `main` tip is ever deployed (Verified-by-construction: `main`
only advances via a Gerrit submit that passed both gates).

### 2. Copy-based build context → self-maintained mirror clone + rsync

The live box (`i-00880b2c7f13527c5`) deploys by **copy**: `/opt/rebar` is a plain copy of the
repo (no `.git`), and it is the compose `build.context` for both services. So autodeploy keeps
its OWN **regular** git clone at `MIRROR_DIR=/var/lib/rebar/mirror` (HTTPS-enforced; a supply-chain
guard aborts if the remote is not `https://`) where all git ops run (fetch, `rev-parse origin/main`,
the `git diff --name-only` component change-detection, checkout), then `rsync -a --delete` the
checked-out source into `/opt/rebar` with hard excludes protecting the SSM-sourced `infra/compose/.env`
(the only runtime state under `/opt/rebar`; all Docker state lives outside it in named volumes +
`/var/gerrit/*` bind mounts). review-bot bakes source at build time, so a rebuild picks up new code.

### 3. v1 auto-apply surface = review-bot container ONLY

replication.config, g2p, and `refs/meta/config` are **DETECT-ONLY** in v1 — a change is signalled
(`AUTODEPLOY_ERROR` marker) for a manual operator apply, never auto-applied. Their correct apply
needs a live-site copy (`/var/gerrit/site/etc/…`) and an SSM PAT re-fetch (`materialize-g2p-config.sh`)
whose failure modes must not sit in the unattended path guarding a fail-closed gate; they are rare
and already hand-applied. Config auto-apply is a documented **v2** follow-up.

### 4. Stability — bounded blast radius, self-heal, SHA-keyed backoff

- **Blast radius:** never touches the `gerrit` container (explicit `review-bot` target + a
  post-deploy assert the gerrit container id is unchanged); never modifies `refs/meta/config`;
  no Gerrit restart.
- **Self-heal:** after `up -d`, an end-to-end health check (liveness: process up + `/health` 200,
  30s) gates success; on failure → rollback to the `:prev` image, `deployed-sha` not advanced.
- **Backoff, never hard-disable:** capped exponential backoff (base 60s, cap 15m) keyed to the
  target SHA — a NEW `main` tip resets it (fix-forward deploys promptly); a known-bad SHA is
  retried no faster than the cap and is never permanently blacklisted. `flock` serialises fires.
- **CI config-gate:** `make config-check` runs in `test.yml`/`gerrit-verify`, so a malformed
  config fails the `Verified` gate and can never reach `main`.

## Back-out

`systemctl disable --now rebar-autodeploy.timer`. The manual deploy path (`compose-up.sh` /
`setup-*.sh`) is unchanged. Units ship DISABLED (`install-autodeploy.sh`); the operator enables
only after a manual dry-run (`systemctl start rebar-autodeploy.service`) is confirmed healthy.

## Consequences

The box converges to `main` within ~poll + deploy time with no human action and no GitHub→AWS
trust surface, while the fail-closed gate is protected by rollback + bounded blast radius. The
cost is a custom deploy loop (Watchtower was rejected — it polls pre-built registry images, not
source rebuilds) and a v1 that still requires a manual apply for the rare config-ref change.

## Amendment (2026-08-24) — the `mcp` autodeploy target uses blue-green pointer-swap, NOT the review-bot stop-and-drain

**Relates to:** ADR 0104 (MCP on the AWS box), ADR 0067 (review-bot bounded shutdown),
`sticky-genetic-narwhal` (local `origin/main`-tracking updater precedent).

Decision 3 above scoped v1 auto-apply to the **review-bot** container, whose deploy is a
stop-and-health-drain (`docker compose up -d` + `/health` in-flight gate, ADR 0067). ADR 0104
adds a `rebar-mcp` HTTP server to this box, and its autodeploy target **deliberately does not
reuse** that stop-and-drain. The `mcp` target is **blue-green**: build the new image → start a
**new** `rebar-mcp` container → health-check it → **atomically flip the nginx `/mcp/` upstream**
to it (the `current`-pointer rename analog) → **retire the old container off the critical path**,
gated by a flock-style "still serving?" check (active connections / its own `in_flight` gauge),
with bounded retention, a hard cap, and an SNS alert when a busy old container cannot be
reclaimed. Overlap is capped **one-in / one-out** (never N parallel), guarded by a
memory-pressure alarm so the transient 2× `rebar-mcp` never forces the `t4g.large` (8 GiB) up to
`t4g.xlarge`.

**Why blue-green here and not the review-bot pattern.** A **shared** MCP server driven by N
agents pipelines long, billable LLM ops, so its `in_flight` gauge may **never** reach 0 (bug
`7b4a` saturation): a gauge-gated drain bound would then be exhausted and **kill an in-flight
certified op**, and a bigger bound only defers the same kill. Blue-green **decouples retirement
from the deploy**, so a never-idle server never blocks a deploy and never forces a kill. In-place
reload is rejected outright because rebar lazy-imports late (e.g. `commit_impact` during `close`,
after the minutes-long completion verifier), so swapping code under a running process risks an
`ImportError` mid-op. A killed MCP op is client-visible (dropped HTTP connection → the agent
retries) and rebar's event-sourced writes are idempotent (an unpersisted op-cert is re-requested),
so retirement is safe. This mirrors the immutable-release + retire-when-idle shape of the local
`~/.local/bin/rebar-dev-update.sh` updater (`sticky-genetic-narwhal`), cited as external local
prior art. The stability constraints of Decision 4 (bounded blast radius, self-heal/rollback,
SHA-keyed backoff, `flock` serialisation) apply unchanged to the `mcp` target.
