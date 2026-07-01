#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install-plugins.sh — install the `events-log` plugin into the Gerrit site.
# Story S4a (review-bot identity + event plumbing).
#
# The `webhooks` plugin is ALREADY BUNDLED + ENABLED in the
# gerritcodereview/gerrit:3.14.1 image, so there is NOTHING to install for it —
# this script only fetches `events-log`, which is NOT bundled.
#
# Steps:
#   1. Download the pinned events-log jar (URL recorded in plugins/README.md).
#   2. Verify its sha256 against the recorded checksum (FAIL on mismatch).
#   3. Place it in the Gerrit site plugins dir.
#   4. Note that Gerrit must be reloaded/restarted to load the new plugin.
#
# Env:
#   GERRIT_SITE        Gerrit site root (default /var/gerrit/site); jar lands in
#                      $GERRIT_SITE/plugins/events-log.jar.
#   EVENTS_LOG_URL     override the download URL (default = the pinned URL below).
#   EVENTS_LOG_SHA256  override the expected sha256 (default = the pinned value).
# ---------------------------------------------------------------------------
set -euo pipefail

# --- Pinned provenance (keep in sync with infra/gerrit/plugins/README.md) ---
EVENTS_LOG_URL="${EVENTS_LOG_URL:-https://gerrit-ci.gerritforge.com/job/plugin-events-log-bazel-master-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/events-log/events-log.jar}"
EVENTS_LOG_SHA256="${EVENTS_LOG_SHA256:-46ef4f8741a733251bdbc7ce80fcdc0cb9885aff13e7895e0038c7c52aec565c}"

GERRIT_SITE="${GERRIT_SITE:-/var/gerrit/site}"
PLUGINS_DIR="${GERRIT_SITE}/plugins"
DEST="${PLUGINS_DIR}/events-log.jar"

# --- 1. Download to a temp file --------------------------------------------
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

echo "install-plugins: downloading events-log from ${EVENTS_LOG_URL}" >&2
curl -fsSL --max-time 120 -o "$tmp" "$EVENTS_LOG_URL"

# --- 2. Verify sha256 (fail-fast on mismatch) ------------------------------
if command -v sha256sum >/dev/null 2>&1; then
	actual="$(sha256sum "$tmp" | awk '{print $1}')"
else
	# macOS / BSD fallback
	actual="$(shasum -a 256 "$tmp" | awk '{print $1}')"
fi

if [ "$actual" != "$EVENTS_LOG_SHA256" ]; then
	echo "install-plugins: SHA256 MISMATCH — refusing to install" >&2
	echo "  expected: ${EVENTS_LOG_SHA256}" >&2
	echo "  actual:   ${actual}" >&2
	exit 1
fi
echo "install-plugins: sha256 verified (${actual})" >&2

# --- 3. Place it in the site plugins dir -----------------------------------
mkdir -p "$PLUGINS_DIR"
# Atomic move into place (temp + mv) so a half-written jar is never observed.
mv "$tmp" "$DEST"
trap - EXIT
chmod 0644 "$DEST"
echo "install-plugins: installed ${DEST}" >&2

# --- 4. Reload note --------------------------------------------------------
# Gerrit loads site plugins at startup (and the `events-log` plugin's SQL/HTTP
# modules need a full load). Restart Gerrit to pick it up, e.g.:
#   docker compose -f infra/compose/docker-compose.yml restart gerrit
# (or `ssh -p 29418 admin@<host> gerrit plugin reload events-log` once it is
# known to Gerrit). Until then the jar is on disk but NOT active.
echo "install-plugins: DONE — restart/reload Gerrit to load events-log" >&2
