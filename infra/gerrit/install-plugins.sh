#!/usr/bin/env bash
# Install checksum-pinned events-log and gerrit-oauth-provider jars absent from
# the Gerrit image. The webhooks plugin is bundled. Pins are in plugins/README.md and may be
# overridden by EVENTS_LOG_URL/_SHA256 or OAUTH_URL/_SHA256.
# OAuth lives on disk as oauth.jar but Gerrit identifies its manifest plugin name.
# It is useful with auth.type=OAUTH. Verify hashes before atomic move and restart
# or reload Gerrit to activate new jars. GERRIT_SITE overrides /var/gerrit/site.
set -euo pipefail

# --- Pinned provenance (keep in sync with infra/gerrit/plugins/README.md) ---
EVENTS_LOG_URL="${EVENTS_LOG_URL:-https://gerrit-ci.gerritforge.com/job/plugin-events-log-bazel-master-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/events-log/events-log.jar}"
EVENTS_LOG_SHA256="${EVENTS_LOG_SHA256:-46ef4f8741a733251bdbc7ce80fcdc0cb9885aff13e7895e0038c7c52aec565c}"

# gerrit-oauth-provider: the CI job name OMITS the `master` segment events-log uses
# (plugin-oauth-bazel-stable-3.14, verified — the -master- form 404s).
OAUTH_URL="${OAUTH_URL:-https://gerrit-ci.gerritforge.com/job/plugin-oauth-bazel-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/oauth/oauth.jar}"
OAUTH_SHA256="${OAUTH_SHA256:-2bcf58a652fe5e513d7a4c73362dfc5d9a3dc697f699a5280416ae6f86d0242f}"

GERRIT_SITE="${GERRIT_SITE:-/var/gerrit/site}"
PLUGINS_DIR="${GERRIT_SITE}/plugins"

# --- sha256 of a file (Linux sha256sum / macOS shasum fallback) ------------
sha256_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | awk '{print $1}'
	else
		shasum -a 256 "$1" | awk '{print $1}'
	fi
}

# --- Download <url>, verify <sha256>, install as plugins/<name> ------------
# Idempotent: if the jar is already present AND matches the expected sha256, skip
# the download (so re-running the script is cheap and offline-tolerant).
install_plugin() {
	local name="$1" url="$2" want="$3" dest="${PLUGINS_DIR}/$1"

	if [ -f "$dest" ] && [ "$(sha256_of "$dest")" = "$want" ]; then
		echo "install-plugins: ${name} already present + verified — skipping" >&2
		return 0
	fi

	local tmp
	tmp="$(mktemp)"
	# shellcheck disable=SC2064
	trap "rm -f '$tmp'" RETURN

	echo "install-plugins: downloading ${name} from ${url}" >&2
	curl -fsSL --max-time 120 -o "$tmp" "$url"

	local got
	got="$(sha256_of "$tmp")"
	if [ "$got" != "$want" ]; then
		echo "install-plugins: SHA256 MISMATCH for ${name} — refusing to install" >&2
		echo "  expected: ${want}" >&2
		echo "  actual:   ${got}" >&2
		return 1
	fi
	echo "install-plugins: ${name} sha256 verified (${got})" >&2

	mkdir -p "$PLUGINS_DIR"
	# Atomic move into place so a half-written jar is never observed by Gerrit.
	mv "$tmp" "$dest"
	chmod 0644 "$dest"
	echo "install-plugins: installed ${dest}" >&2
}

install_plugin "events-log.jar" "$EVENTS_LOG_URL" "$EVENTS_LOG_SHA256"
install_plugin "oauth.jar" "$OAUTH_URL" "$OAUTH_SHA256"

# --- Reload note -----------------------------------------------------------
# Gerrit loads site plugins at startup. Restart Gerrit to pick up newly-dropped
# jars, e.g.:  docker compose -f infra/compose/docker-compose.yml restart gerrit
# The oauth plugin in particular MUST be present before Gerrit boots with
# auth.type = OAUTH, or the boot fails ("OAUTH provider not configured").
echo "install-plugins: DONE — restart/reload Gerrit to load newly-installed plugins" >&2
