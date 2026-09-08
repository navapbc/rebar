#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SITE_HOST_DIR="${2:-/var/gerrit/site}"
GERRIT_OWNER="${3:-1000:1000}"

install -d "${SITE_HOST_DIR}/etc"
cp "${REPO_ROOT}/infra/compose/jgit.config" "${SITE_HOST_DIR}/etc/jgit.config"

if [ "$(git config --file "${SITE_HOST_DIR}/etc/jgit.config" --get receive.autogc || true)" != "false" ]; then
  echo "materialize-gerrit-jgit-config: FATAL — receive.autogc must be false" >&2
  exit 1
fi

chown "${GERRIT_OWNER}" "${SITE_HOST_DIR}/etc/jgit.config"
