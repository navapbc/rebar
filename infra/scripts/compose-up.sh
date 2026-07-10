#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# compose-up.sh — boot orchestrator for the Gerrit + review-bot stack (story S2).
#
# Brings up the docker-compose stack on the AL2023 box: ensures Docker + the compose
# plugin are present, creates the persistent Gerrit data volume as a bind onto the
# mounted EBS data volume, regenerates the secrets .env from SSM, then `up -d --build`.
# nginx + certbot are HOST services installed separately (install-certbot-timer.sh).
#
# Run from the repo root. Idempotent: safe to re-run (volume create is guarded,
# the .env is regenerated, compose reconciles to the desired state).
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/compose/docker-compose.yml"
GERRIT_IMAGE="gerritcodereview/gerrit:3.14.1"
# The official image runs from the baked site /var/gerrit and ignores GERRIT_SITE,
# so we persist only the STATEFUL SUBDIRS on the EBS-backed host path /var/gerrit/site.
SITE_HOST_DIR="/var/gerrit/site"
GERRIT_UID=1000 # the `gerrit` user inside the image

# The stateful site subdirs — the SINGLE source of truth for what gets a host dir
# AND an external bind volume. Every `external: true` volume in docker-compose.yml
# must be derivable from this list (CI enforces the pairing: config-check.sh check 5
# diffs the compose file's external volumes against `--print-volumes` output, so a
# compose edit that adds a volume without extending this list cannot reach main —
# the incident-2731 drift class).
SITE_SUBDIRS="git index cache db etc logs plugins reviewbot reviewbot-tickets"

# Volume name for a site subdir: docker volume names cannot carry the hyphenated
# host-dir spelling one-for-one (gerrit_reviewbot_tickets binds reviewbot-tickets),
# so the derivation lives here, once: gerrit_ prefix + hyphens -> underscores.
volume_for_subdir() { printf 'gerrit_%s\n' "${1//-/_}"; }

cd "${REPO_ROOT}"

# --- 1. Ensure Docker + the compose plugin are installed and running -------
# AL2023: docker is in the default repos; the compose v2 plugin ships as a separate
# package. Install both, then enable+start the daemon.
if ! command -v docker >/dev/null 2>&1; then
  echo "compose-up: installing docker..." >&2
  dnf install -y docker
fi
systemctl enable --now docker

# The compose v2 plugin (`docker compose`). On AL2023 it is the docker-compose-plugin
# package; if that is unavailable, drop the plugin binary into the CLI plugins dir.
if ! docker compose version >/dev/null 2>&1; then
  echo "compose-up: installing the docker compose plugin..." >&2
  dnf install -y docker-compose-plugin || {
    mkdir -p /usr/libexec/docker/cli-plugins
    arch="$(uname -m)" # aarch64 on the t4g box
    curl -fsSL \
      "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}" \
      -o /usr/libexec/docker/cli-plugins/docker-compose
    chmod +x /usr/libexec/docker/cli-plugins/docker-compose
  }
fi

# git is used below to MERGE the OAuth creds into gerrit.config/secure.config (WS8) —
# ensure it is present on the minimal AL2023 host (same pattern as the docker install).
if ! command -v git >/dev/null 2>&1; then
  echo "compose-up: installing git..." >&2
  dnf install -y git
fi

# --- 2. Seed the persistent Gerrit site subdirs on the EBS data volume -----
# Create the stateful subdirs (idempotent), seed etc/gerrit.config from the repo,
# copy the image's baked plugins on first run (so an empty mounted plugins dir does
# not hide them — S4a then drops webhooks/events-log here and they persist), and
# chown to the in-image gerrit uid so the container can write.
for d in ${SITE_SUBDIRS}; do
  mkdir -p "${SITE_HOST_DIR}/${d}"
done

# Create the EXTERNAL named volumes the compose file references — one per stateful
# subdir, each a local `bind` volume onto the EBS-backed host path. external:true in
# the compose file means `docker compose down -v` cannot destroy them. Idempotent:
# `volume inspect || volume create`.
for d in ${SITE_SUBDIRS}; do
  vol="$(volume_for_subdir "${d}")"
  docker volume inspect "${vol}" >/dev/null 2>&1 || \
    docker volume create \
      --driver local \
      --opt type=none \
      --opt o=bind \
      --opt device="${SITE_HOST_DIR}/${d}" \
      "${vol}" >/dev/null
done

# Copy the baked plugins ONCE (only if the persistent plugins dir is empty), so we
# keep the image's bundled plugins (incl. replication, used by S5) while still
# persisting any plugins S4a/WS8 add later (events-log, oauth.jar) via the separate
# install-plugins.sh step.
if [ -z "$(ls -A "${SITE_HOST_DIR}/plugins" 2>/dev/null)" ]; then
  echo "compose-up: seeding baked plugins into ${SITE_HOST_DIR}/plugins" >&2
  # NOTE: the Gerrit image has an ENTRYPOINT, so we MUST override it with
  # --entrypoint sh (otherwise `docker run image sh -c ...` boots Gerrit and hangs).
  docker run --rm --entrypoint sh -v "${SITE_HOST_DIR}/plugins:/seed" "${GERRIT_IMAGE}" \
    -c 'cp -a /var/gerrit/plugins/. /seed/ 2>/dev/null || true'
fi

# Ensure the `hooks` core plugin specifically (epic 1fa8 / ADR-0022). g2p's CI dispatch
# relies on the `hooks` plugin exec'ing $site/hooks/*. The first-run bulk seed above
# copies it, but assert it EXPLICITLY + idempotently (every boot) so an already-seeded
# box that predates g2p also gets it — copy hooks.jar from the image if absent.
if [ ! -f "${SITE_HOST_DIR}/plugins/hooks.jar" ]; then
  echo "compose-up: enabling the hooks core plugin (epic 1fa8)" >&2
  docker run --rm --entrypoint sh -v "${SITE_HOST_DIR}/plugins:/seed" "${GERRIT_IMAGE}" \
    -c 'cp -a /var/gerrit/plugins/hooks.jar /seed/ 2>/dev/null || true'
fi

# --- 3. Regenerate the secrets .env from SSM (fail-fast on SSM unreachable) -
# BEFORE seeding gerrit.config, so the OAuth client-id/secret (WS8) can be materialized
# into the live config from SSM.
bash "${SCRIPT_DIR}/fetch-secrets.sh"
ENV_FILE="${REPO_ROOT}/infra/compose/.env"

# --- Seed gerrit.config + materialize OAuth creds (WS8) --------------------
# Always refresh gerrit.config from the repo so config changes deploy, then (when the
# config selects auth.type = OAUTH) set the non-secret client-id in gerrit.config and
# the secret client-secret in etc/secure.config. Both are written with `git config
# --file` (Gerrit configs ARE git-config files): a MERGE that (a) is immune to value
# metacharacters, and (b) preserves any other keys Gerrit itself stores in secure.config
# (e.g. registerEmailPrivateKey) instead of truncating them. Secrets NEVER land in
# gerrit.config.
#
# Plugin install is a SEPARATE, operator-run step (infra/gerrit/install-plugins.sh —
# runbook Step 3), so a whole-stack boot is not coupled to GerritForge CI reachability.
# We only VERIFY oauth.jar is present here (fail-loud) before booting into OAUTH.
cp "${REPO_ROOT}/infra/compose/gerrit.config" "${SITE_HOST_DIR}/etc/gerrit.config"

oauth_client_id="$(grep -E '^GITHUB_OAUTH_CLIENT_ID=' "${ENV_FILE}" | cut -d= -f2-)"
oauth_client_secret="$(grep -E '^GITHUB_OAUTH_CLIENT_SECRET=' "${ENV_FILE}" | cut -d= -f2-)"

if grep -qE '^[[:space:]]*type[[:space:]]*=[[:space:]]*OAUTH' "${SITE_HOST_DIR}/etc/gerrit.config"; then
  # Fail LOUD rather than boot a half-configured OAUTH Gerrit.
  [ -f "${SITE_HOST_DIR}/plugins/oauth.jar" ] || {
    echo "compose-up: FATAL — auth.type = OAUTH but plugins/oauth.jar is absent (run infra/gerrit/install-plugins.sh first)" >&2
    exit 1; }
  [ -n "${oauth_client_id}" ] && [ -n "${oauth_client_secret}" ] || {
    echo "compose-up: FATAL — auth.type = OAUTH but OAuth client-id/secret missing from ${ENV_FILE}" >&2
    exit 1; }

  gerrit_cfg="${SITE_HOST_DIR}/etc/gerrit.config"
  secure_cfg="${SITE_HOST_DIR}/etc/secure.config"
  oauth_section="plugin.gerrit-oauth-provider-github-oauth"

  git config --file "${gerrit_cfg}" "${oauth_section}.client-id" "${oauth_client_id}"

  # Merge the secret into secure.config (create at 0600 if absent, preserve Gerrit's
  # own keys if present), then re-assert 0600 regardless of the pre-existing mode.
  [ -f "${secure_cfg}" ] || { (umask 077; : >"${secure_cfg}"); }
  git config --file "${secure_cfg}" "${oauth_section}.client-secret" "${oauth_client_secret}"
  chmod 600 "${secure_cfg}"
  echo "compose-up: set OAuth client-id in gerrit.config, merged client-secret into secure.config (0600)" >&2
fi

chown -R "${GERRIT_UID}:${GERRIT_UID}" "${SITE_HOST_DIR}"

# --- Materialize the gerrit-to-platform CI config (epic 1fa8 / story S3) ----
# g2p (in the Gerrit container) reads its GitHub PAT + config from a bind-mounted dir;
# materialize it from SSM at boot, the same way the replication deploy key is. NON-FATAL:
# a missing PAT must not block the whole stack from booting — g2p just won't dispatch CI
# and the gate stays fail-closed (no Verified vote -> no submit) until it is fixed.
if ! bash "${REPO_ROOT}/infra/gerrit/materialize-g2p-config.sh"; then
  echo "compose-up: WARN — g2p config materialization failed; CI dispatch disabled until fixed" >&2
fi

# --- 4. Bring the stack up (build the review-bot image, pull Gerrit) -------
docker compose -f "${COMPOSE_FILE}" up -d --build

echo "compose-up: stack is up (gerrit + review-bot). nginx/certbot are host services." >&2
