# ADR 0026 — Continuous auto-deploy: on-box systemd timer, copy-based build context, v1 = review-bot only

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
