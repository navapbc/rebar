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

# --- 2. Seed the persistent Gerrit site subdirs on the EBS data volume -----
# Create the stateful subdirs (idempotent), seed etc/gerrit.config from the repo,
# copy the image's baked plugins on first run (so an empty mounted plugins dir does
# not hide them — S4a then drops webhooks/events-log here and they persist), and
# chown to the in-image gerrit uid so the container can write.
for d in git index cache db etc logs plugins; do
  mkdir -p "${SITE_HOST_DIR}/${d}"
done

# Seed gerrit.config (always refresh from the repo so config changes deploy).
cp "${REPO_ROOT}/infra/compose/gerrit.config" "${SITE_HOST_DIR}/etc/gerrit.config"

# Copy the baked plugins ONCE (only if the persistent plugins dir is empty), so we
# keep the image's bundled plugins (incl. replication, used by S5) while still
# persisting any plugins S4a adds later.
if [ -z "$(ls -A "${SITE_HOST_DIR}/plugins" 2>/dev/null)" ]; then
  echo "compose-up: seeding baked plugins into ${SITE_HOST_DIR}/plugins" >&2
  # NOTE: the Gerrit image has an ENTRYPOINT, so we MUST override it with
  # --entrypoint sh (otherwise `docker run image sh -c ...` boots Gerrit and hangs).
  docker run --rm --entrypoint sh -v "${SITE_HOST_DIR}/plugins:/seed" "${GERRIT_IMAGE}" \
    -c 'cp -a /var/gerrit/plugins/. /seed/ 2>/dev/null || true'
fi

chown -R "${GERRIT_UID}:${GERRIT_UID}" "${SITE_HOST_DIR}"

# --- 3. Regenerate the secrets .env from SSM (fail-fast on SSM unreachable) -
bash "${SCRIPT_DIR}/fetch-secrets.sh"

# --- 4. Bring the stack up (build the review-bot image, pull Gerrit) -------
docker compose -f "${COMPOSE_FILE}" up -d --build

echo "compose-up: stack is up (gerrit + review-bot). nginx/certbot are host services." >&2
