# ADR 0008: Secrets are sourced from SSM into an ephemeral 0600 .env each boot

- **Status:** Accepted
- **Context:** Epic *stand up AWS-hosted Gerrit + rebar review-bot (PoC)* (`d251`),
  story *S2 — app config / deploy*. Builds on S1 (ADR-0012), which provisioned the
  EC2 instance role and the `/rebar/prod/*` SSM SecureString slots.

## Context

The containers need secrets at runtime — the Anthropic API key, the MCP HMAC
signing key, the Gerrit admin password, and the Gerrit bot token. These must never
be committed to the repo, baked into an image layer, or left lying around in a
stale file that could drift from the source of truth.

S1 already established the secure substrate:

- The secrets live as **SSM Parameter Store SecureString** params under
  `/rebar/prod/*`, encrypted with the AWS-managed SSM KMS key.
- The EC2 **instance role** (`rebar-gerrit-instance-role`) grants scoped read of
  `/rebar/prod/*` plus `kms:Decrypt` via SSM — so the box can read its own secrets
  with **no static AWS keys** anywhere on disk.

docker-compose consumes runtime config most naturally from an `env_file`/`.env`. The
question is how that `.env` comes to exist without committing secrets or trusting a
stale copy.

## Decision

Generate the `.env` **fresh on each boot** from SSM, via the instance role:

- `infra/scripts/fetch-secrets.sh` reads the **subset** of `/rebar/prod/*` params the
  containers actually need (documented leaf→env mapping in the script header) and
  writes `infra/compose/.env` with **mode 0600**, authenticating purely through the
  instance role (region discovered via IMDSv2, no access keys).
- The script is **idempotent** (overwrites) and **fail-fast**: if any
  `aws ssm get-parameter` fails (SSM unreachable, param missing), it aborts with
  exit 1 **before** touching the existing `.env` — so a partial or stale secrets
  file is never left in place. All params are fetched into shell vars first; the
  `.env` is written atomically (temp file → `mv`) only after every read succeeds.
- `infra/compose/.env` is **git-ignored** (and excluded from the Docker build
  context), so secrets never reach the repo or an image layer.
- `compose-up.sh` runs `fetch-secrets.sh` as a boot step before `docker compose up`.

## Consequences

- **No static credentials** exist on the box — the instance role is the only
  authority, and rotating a secret in SSM is picked up on the next boot/regeneration
  with no redeploy.
- **No stale-secret risk**: fail-fast means a broken SSM read stops the boot loudly
  rather than silently running on an old `.env`.
- **Single source of truth** is SSM; the `.env` is a disposable cache. The 0600 mode
  + git-ignore + build-context exclusion keep it off disk-readable-by-others, out of
  the repo, and out of image layers.
- Only the **needed** leaves are fetched (least exposure): the SSH host key, the
  replication deploy key, and the alert endpoint are consumed by other components,
  not these containers, so they are not written into this `.env`.
