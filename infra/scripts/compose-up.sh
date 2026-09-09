#!/usr/bin/env bash
# Idempotently provision and start the Gerrit, review-bot, op-cert, and MCP compose
# services on AL2023. Persistent state binds to EBS; nginx and certbot remain host services.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/compose/docker-compose.yml"
GERRIT_IMAGE="gerritcodereview/gerrit:3.14.1"
# Persist the official image's stateful /var/gerrit subdirectories on EBS.
SITE_HOST_DIR="/var/gerrit/site"
GERRIT_UID=1000 # the `gerrit` user inside the image

# Single source for EBS host directories and external bind volumes; config-check.sh
# compares it with docker-compose.yml through --print-volumes.
SITE_SUBDIRS="git index cache db etc logs plugins reviewbot reviewbot-tickets mcp-tickets mcp-code"

# Derive volume names once: add gerrit_ and replace hyphens with underscores.
volume_for_subdir() { printf 'gerrit_%s\n' "${1//-/_}"; }

# Side-effect-free volume enumeration must precede every provisioning action.
if [ "${1:-}" = "--print-volumes" ]; then
  for d in ${SITE_SUBDIRS}; do volume_for_subdir "${d}"; done
  exit 0
fi

cd "${REPO_ROOT}"

# 1. Install Docker and its compose plugin, then start the daemon.
if ! command -v docker >/dev/null 2>&1; then
  echo "compose-up: installing docker..." >&2
  dnf install -y docker
fi

# Install BuildKit's cap before first daemon start. Re-runs report whether a live daemon
# has loaded it but never restart Docker. Failure is a monitored capacity problem, not a boot gate.
if ! bash "${SCRIPT_DIR}/docker-storage-cap.sh" --install; then
  echo "compose-up: WARN — the Docker storage cap was not installed; BuildKit build cache is UNBOUNDED until fixed (see infra/runbooks/review-bot-ops.md)" >&2
fi

# Install journald's cap only when restart safety is proven, then observe whether it is live.
# Failure remains a monitored capacity problem and does not block the stack.
if ! bash "${SCRIPT_DIR}/journald-cap.sh" --install; then
  echo "compose-up: WARN — the journald disk ceiling was not installed; the journal is bounded only by systemd's derived default (see infra/runbooks/review-bot-ops.md)" >&2
fi

# Install /var/tmp cleanup and a quota only when XFS project accounting is active.
# Failure remains a monitored capacity problem and does not block the stack.
if ! bash "${SCRIPT_DIR}/vartmp-cap.sh" --install; then
  echo "compose-up: WARN — the /var/tmp bound was not installed; /var/tmp is bounded only by the size of the root volume (see infra/runbooks/review-bot-ops.md)" >&2
fi

# Install the reaper that can remove EXITED containers. Without it, container-layer
# growth is a monitored capacity problem; this installer remains non-fatal.
if ! bash "${SCRIPT_DIR}/container-cap.sh" --install; then
  echo "compose-up: WARN — the writable-container-layer bound was not installed; exited-container debris is bounded only by the size of the root volume (see infra/runbooks/review-bot-ops.md)" >&2
fi

systemctl enable --now docker

# Fall back to the compose plugin binary when AL2023's package is unavailable.
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

# Git merges OAuth values into Gerrit's config files below.
if ! command -v git >/dev/null 2>&1; then
  echo "compose-up: installing git..." >&2
  dnf install -y git
fi

# 2. Create the persistent Gerrit site directories on EBS.
for d in ${SITE_SUBDIRS}; do
  mkdir -p "${SITE_HOST_DIR}/${d}"
done

# Bind one external named volume to each EBS-backed state directory.
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

# 2b. Create only gate-scratch's parent. Never create the mount point itself on root;
# the mount marker controls gate admission when the dedicated volume is absent.
SCRATCH_MOUNT="${GATE_SCRATCH_MOUNT:-/var/lib/rebar/gate-scratch}"
mkdir -p "$(dirname "${SCRATCH_MOUNT}")"
if [ ! -f "${SCRATCH_MOUNT}/.gate-scratch-mounted" ]; then
  echo "compose-up: WARNING — ${SCRATCH_MOUNT} carries no .gate-scratch-mounted marker." >&2
  echo "compose-up:   The gate-scratch volume is not mounted. Gate runs will REFUSE rather" >&2
  echo "compose-up:   than write to the root filesystem. See infra/runbooks/review-bot-ops.md." >&2
fi

# Seed baked plugins only into an empty persistent directory.
if [ -z "$(ls -A "${SITE_HOST_DIR}/plugins" 2>/dev/null)" ]; then
  echo "compose-up: seeding baked plugins into ${SITE_HOST_DIR}/plugins" >&2
  # Override the image entrypoint so this copies files instead of starting Gerrit.
  docker run --rm --entrypoint sh -v "${SITE_HOST_DIR}/plugins:/seed" "${GERRIT_IMAGE}" \
    -c 'cp -a /var/gerrit/plugins/. /seed/ 2>/dev/null || true'
fi

# Ensure hooks.jar exists even on sites seeded before hook-based CI dispatch.
if [ ! -f "${SITE_HOST_DIR}/plugins/hooks.jar" ]; then
  echo "compose-up: enabling the hooks core plugin (epic 1fa8)" >&2
  docker run --rm --entrypoint sh -v "${SITE_HOST_DIR}/plugins:/seed" "${GERRIT_IMAGE}" \
    -c 'cp -a /var/gerrit/plugins/hooks.jar /seed/ 2>/dev/null || true'
fi

# 3. Fetch secrets before materializing OAuth configuration.
bash "${SCRIPT_DIR}/fetch-secrets.sh"
ENV_FILE="${REPO_ROOT}/infra/compose/.env"

# Refresh gerrit.config, then merge OAuth id and secret into their separate Git-config
# files without discarding Gerrit's own secure keys. OAuth mode requires oauth.jar and
# both values; plugin installation remains an operator step.
cp "${REPO_ROOT}/infra/compose/gerrit.config" "${SITE_HOST_DIR}/etc/gerrit.config"
bash "${REPO_ROOT}/infra/scripts/materialize-gerrit-jgit-config.sh" "${REPO_ROOT}" "${SITE_HOST_DIR}" "${GERRIT_UID}:${GERRIT_UID}"

oauth_client_id="$(grep -E '^GITHUB_OAUTH_CLIENT_ID=' "${ENV_FILE}" | cut -d= -f2-)"
oauth_client_secret="$(grep -E '^GITHUB_OAUTH_CLIENT_SECRET=' "${ENV_FILE}" | cut -d= -f2-)"

if grep -qE '^[[:space:]]*type[[:space:]]*=[[:space:]]*OAUTH' "${SITE_HOST_DIR}/etc/gerrit.config"; then
  # Refuse a partially configured OAuth boot.
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

  # Preserve existing secure.config keys and enforce mode 0600.
  [ -f "${secure_cfg}" ] || { (umask 077; : >"${secure_cfg}"); }
  git config --file "${secure_cfg}" "${oauth_section}.client-secret" "${oauth_client_secret}"
  chmod 600 "${secure_cfg}"
  echo "compose-up: set OAuth client-id in gerrit.config, merged client-secret into secure.config (0600)" >&2
fi

chown -R "${GERRIT_UID}:${GERRIT_UID}" "${SITE_HOST_DIR}"

# Materialize g2p configuration non-fatally; missing CI dispatch leaves Verified fail-closed.
if ! bash "${REPO_ROOT}/infra/gerrit/materialize-g2p-config.sh"; then
  echo "compose-up: WARN — g2p config materialization failed; CI dispatch disabled until fixed" >&2
fi

# Materialize the op-cert guard non-fatally; failure keeps /opcert/ at deny-all.
if ! bash "${REPO_ROOT}/infra/scripts/materialize-opcert-guard.sh"; then
  echo "compose-up: WARN — op-cert guard materialization failed; /opcert/ stays fail-closed (403) until fixed" >&2
fi

# Seed the MCP upstream non-fatally before compose; preserve any blue-green target.
if ! bash "${REPO_ROOT}/infra/scripts/materialize-mcp-upstream.sh"; then
  echo "compose-up: WARN — MCP upstream materialization failed; /mcp returns 502 until fixed" >&2
fi

# 4. Reconcile the compose stack.
docker compose -f "${COMPOSE_FILE}" up -d --build

echo "compose-up: stack is up (gerrit + review-bot + opcert + mcp). nginx/certbot are host services." >&2
