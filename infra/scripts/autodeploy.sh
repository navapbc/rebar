#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# autodeploy.sh — continuous auto-deploy: make the running Gerrit box reflect `main`
# without manual deploy or restart (epic 88ab / story 8903).
#
# Run every ~2 min by rebar-autodeploy.timer -> rebar-autodeploy.service (oneshot).
# Polls the PUBLIC GitHub mirror read-only (no GitHub->AWS trust surface) and, when
# `main` advances, redeploys the review-bot container IFF its source changed.
#
# BOX-ADAPTATION (grounded in live box i-00880b2c7f13527c5): the compose build context
# `/opt/rebar` is a COPY of the repo, not a git checkout. So autodeploy keeps its OWN
# regular git clone at $MIRROR_DIR (all git ops run there), then `rsync`s the checked-out
# source into $DEPLOY_REPO (excluding the SSM-sourced .env + Docker state), then rebuilds
# the review-bot image from that build context.
#
# v1 AUTO-APPLY SURFACE = review-bot container ONLY. replication.config / g2p /
# refs/meta/config changes are DETECT-ONLY (signalled for a manual operator apply): their
# correct apply needs a live-site copy + an SSM PAT re-fetch whose failure modes must not
# sit in the unattended path guarding a fail-closed gate. (v2 follow-up.)
#
# STABILITY (the box runs a FAIL-CLOSED gate — a bad deploy could freeze submissions):
#   - bounded blast radius: NEVER touches the `gerrit` container; only the review-bot
#     service is rebuilt/restarted; config refs are never auto-applied.
#   - self-heal: an end-to-end health check gates success; on failure the review-bot is
#     ROLLED BACK to its `:prev` image so the gate is restored; deployed-sha not advanced.
#   - capped exponential backoff (NOT hard-disable), keyed to the target SHA: a new `main`
#     tip RESETS the backoff (fix-forward deploys promptly); a known-bad SHA is retried no
#     faster than the cap. (Flux retryInterval, Argo CD retry backoff, systemd RestartSteps.)
#   - flock: overlapping timer fires never overlap.
#   - config-check runs at CI (make config-check) so a malformed config never reaches `main`.
# ---------------------------------------------------------------------------
set -uo pipefail   # NOT -e: we handle failures explicitly (fail-safe, never half-updated)

# ── tunables (single source of truth; overridable via env / /etc/rebar/autodeploy.env) ──
[ -f /etc/rebar/autodeploy.env ] && . /etc/rebar/autodeploy.env
DEPLOY_REPO="${DEPLOY_REPO:-/opt/rebar}"              # the compose build context (a COPY, not git)
COMPOSE_DIR="${COMPOSE_DIR:-$DEPLOY_REPO/infra/compose}"
MIRROR_DIR="${MIRROR_DIR:-/var/lib/rebar/mirror}"     # autodeploy's OWN regular git clone
MIRROR_URL="${MIRROR_URL:-https://github.com/navapbc/rebar.git}"   # PUBLIC mirror (read-only, HTTPS)
MIRROR_REMOTE="${MIRROR_REMOTE:-origin}"
STATE_DIR="${STATE_DIR:-/var/lib/rebar}"
LOCK="$STATE_DIR/deploy.lock"
SHA_FILE="$STATE_DIR/deployed-sha"
BACKOFF_FILE="$STATE_DIR/deploy-backoff"              # "<target-sha> <fail-count> <next-epoch>"
BOT_SERVICE="${BOT_SERVICE:-review-bot}"              # compose service name (NEVER 'gerrit')
BOT_IMAGE="${BOT_IMAGE:-compose-review-bot}"
GERRIT_CONTAINER="${GERRIT_CONTAINER:-compose-gerrit-1}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"   # review-bot receiver (NOT Gerrit 8080)
FETCH_TIMEOUT="${FETCH_TIMEOUT:-60}"                  # a hung fetch must not hold the lock
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-30}"
BACKOFF_BASE="${BACKOFF_BASE:-60}"; BACKOFF_FACTOR="${BACKOFF_FACTOR:-2}"; BACKOFF_CAP="${BACKOFF_CAP:-900}"
BUILD_CACHE_KEEP="${BUILD_CACHE_KEEP:-5GB}"           # buildkit cache hard cap (docker builder prune --keep-storage)

# review-bot redeploys iff a matching path changed between deployed..target.
BOT_PATHS='src/rebar/ infra/compose/Dockerfile.reviewbot pyproject.toml infra/compose/docker-compose.yml'
# config paths are DETECT-ONLY in v1 (signalled, never auto-applied).
CONFIG_PATHS='infra/gerrit/replication.config infra/gerrit/project.config infra/gerrit/gerrit_to_platform.ini.template infra/gerrit/materialize-g2p-config.sh'
# host observability probe: re-materialized (idempotent installer) on a source change.
# Its installed copy at /usr/local/bin lives OUTSIDE the compose build context, so a probe
# change reaches no trigger above and would otherwise never be refreshed on the box.
OBS_PATHS='infra/scripts/observability.sh infra/scripts/install-observability.sh'
# rsync excludes: protect the SSM secrets .env, the deploy marker, and dev/state dirs.
RSYNC_EXCLUDES=(--exclude '/.git' --exclude 'infra/compose/.env' --exclude '/.deployed_ref' \
  --exclude '/.venv' --exclude '/.terraform' --exclude '/.serena' --exclude '/.claude' --exclude '/.tickets-tracker')

mkdir -p "$STATE_DIR"
now() { date +%s; }
log() { printf '{"event":"autodeploy","ts":%s,"msg":%s}\n' "$(now)" "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$*")"; }
err() { printf 'AUTODEPLOY_ERROR %s\n' "$(python3 -c 'import json,sys;print(json.dumps({"ts":int(sys.argv[1]),"reason":sys.argv[2],"detail":sys.argv[3]}))' "$(now)" "$1" "${2:-}")" >&2; }

# ── single-flight ─────────────────────────────────────────────────────────────
exec 9>"$LOCK"
flock -n 9 || { log "another deploy holds the lock; skipping"; exit 0; }

# ── mirror clone: self-bootstrap + HTTPS supply-chain guard ───────────────────
if [ ! -d "$MIRROR_DIR/.git" ]; then
  log "bootstrapping mirror clone at $MIRROR_DIR from $MIRROR_URL"
  mkdir -p "$(dirname "$MIRROR_DIR")"
  if ! git clone -q "$MIRROR_URL" "$MIRROR_DIR" 2>/dev/null; then
    err mirror-clone-failed "git clone $MIRROR_URL -> $MIRROR_DIR failed"; exit 1
  fi
fi
remote_url="$(git -C "$MIRROR_DIR" remote get-url "$MIRROR_REMOTE" 2>/dev/null || true)"
case "$remote_url" in
  https://*) : ;;
  *) err mirror-not-https "mirror remote is '$remote_url' (must be https:// — supply-chain guard)"; exit 1 ;;
esac

# ── fetch the target tip (bounded), key backoff to it ─────────────────────────
if ! timeout "$FETCH_TIMEOUT" git -C "$MIRROR_DIR" fetch -q --prune "$MIRROR_REMOTE" main 2>/dev/null; then
  err fetch_failed "git fetch $MIRROR_REMOTE main timed out/failed (mirror may be stalling)"; exit 1
fi
TARGET="$(git -C "$MIRROR_DIR" rev-parse "$MIRROR_REMOTE/main")"
DEPLOYED="$(cat "$SHA_FILE" 2>/dev/null || true)"

# First run: adopt current state WITHOUT deploying (no :prev exists yet). Seed from the
# box's existing deploy marker if present, else the mirror tip.
if [ -z "$DEPLOYED" ]; then
  seed="$TARGET"
  if [ -f "$DEPLOY_REPO/.deployed_ref" ]; then
    ref="$(awk '{print $1}' "$DEPLOY_REPO/.deployed_ref" 2>/dev/null)"
    git -C "$MIRROR_DIR" rev-parse --verify -q "$ref^{commit}" >/dev/null 2>&1 && seed="$(git -C "$MIRROR_DIR" rev-parse "$ref")"
  fi
  echo "$seed" > "$SHA_FILE.tmp" && mv "$SHA_FILE.tmp" "$SHA_FILE"
  log "first run: adopting $seed as deployed-sha (no deploy)"; exit 0
fi
[ "$TARGET" = "$DEPLOYED" ] && { log "up to date ($TARGET); no-op"; exit 0; }

# backoff: same failed TARGET, not time yet -> skip. NEW target -> reset (fix-forward).
read -r bo_sha bo_cnt bo_next < <(cat "$BACKOFF_FILE" 2>/dev/null || echo "- 0 0")
if [ "$bo_sha" = "$TARGET" ] && [ "$(now)" -lt "${bo_next:-0}" ]; then
  log "backoff active for $TARGET (fail #$bo_cnt); next attempt at $bo_next"; exit 0
fi
[ "$bo_sha" != "$TARGET" ] && { bo_cnt=0; }

# Reclaim docker garbage, best-effort (incident 2731: a failing rebuild loop left
# multi-GB buildkit cache + dangling layers on the 30G root disk until ENOSPC
# fail-closed the gate — and the failure path had NO reclamation at all). Bounded:
# the buildkit cache is hard-capped at BUILD_CACHE_KEEP (keeps a warm cache for
# fast rebuilds), dangling images are dropped; TAGGED images are never touched
# (:prev is the rollback lifeline). Each prune is time-bounded (a wedged daemon
# under disk pressure must not hold the deploy lock) and can NEVER alter control
# flow or mask the caller's failure exit code — a prune failure only logs.
prune_docker_caches() {
  if ! timeout 120 docker builder prune -f --keep-storage "$BUILD_CACHE_KEEP" >/dev/null 2>&1; then
    log "prune_docker_caches: builder prune failed (non-fatal)"
  fi
  if ! timeout 120 docker image prune -f >/dev/null 2>&1; then
    log "prune_docker_caches: image prune failed (non-fatal)"
  fi
  return 0
}

record_backoff_failure() {
  local n=$(( ${bo_cnt:-0} + 1 ))
  local wait=$(( BACKOFF_BASE * (BACKOFF_FACTOR ** (n-1)) )); [ "$wait" -gt "$BACKOFF_CAP" ] && wait=$BACKOFF_CAP
  echo "$TARGET $n $(( $(now) + wait ))" > "$BACKOFF_FILE"
  err deploy_failed "target=$TARGET fail#$n backoff=${wait}s"
  log "deploy failed; backoff ${wait}s (fail #$n); last-known-good stays live"
  prune_docker_caches
}
clear_backoff() { rm -f "$BACKOFF_FILE"; }

# ── what changed? (computed in the mirror clone) ──────────────────────────────
changed() { git -C "$MIRROR_DIR" diff --name-only "$DEPLOYED" "$TARGET" -- $1 2>/dev/null | grep -q .; }
log "main advanced $DEPLOYED -> $TARGET; computing component deltas"

# ── config refs (replication/g2p/meta): DETECT-ONLY (v1 boundary) ─────────────
if changed "$CONFIG_PATHS"; then
  err config_manual "infra config changed in $TARGET — replication/g2p/refs-meta need a MANUAL operator apply (auto-apply is a v2 follow-up)"
  log "infra config change detected + signalled (not auto-applied in v1)"
fi

# ── review-bot: rebuild + restart ONLY on a source change ─────────────────────
if changed "$BOT_PATHS"; then
  log "review-bot sources changed; sync + rebuild + restart (blast radius = $BOT_SERVICE only)"
  # sync the target source into the copy-based build context (git checkout in the MIRROR).
  if ! git -C "$MIRROR_DIR" checkout -q "$TARGET" 2>/dev/null; then
    err mirror-checkout-failed "git checkout $TARGET in $MIRROR_DIR failed"; record_backoff_failure; exit 1
  fi
  if ! rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$MIRROR_DIR/" "$DEPLOY_REPO/" 2>/dev/null; then
    err rsync-failed "rsync $MIRROR_DIR -> $DEPLOY_REPO failed"; record_backoff_failure; exit 1
  fi
  # keep the copy owned by the deploy user; the excluded secrets .env keeps its own owner/perms.
  env_owner="$(stat -c '%U:%G' "$DEPLOY_REPO/infra/compose/.env" 2>/dev/null || true)"
  chown -R 502:502 "$DEPLOY_REPO" 2>/dev/null || true
  [ -n "$env_owner" ] && chown "$env_owner" "$DEPLOY_REPO/infra/compose/.env" 2>/dev/null || true

  gerrit_before="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"
  # preserve the current image as :prev for rollback (only if one exists).
  if docker image inspect "$BOT_IMAGE:latest" >/dev/null 2>&1; then docker tag "$BOT_IMAGE:latest" "$BOT_IMAGE:prev"; have_prev=1; else have_prev=0; fi
  if ! ( cd "$COMPOSE_DIR" && docker compose build "$BOT_SERVICE" && docker compose up -d "$BOT_SERVICE" ); then
    err bot-build-failed "compose build/up $BOT_SERVICE failed"
    [ "$have_prev" = 1 ] && { docker tag "$BOT_IMAGE:prev" "$BOT_IMAGE:latest"; ( cd "$COMPOSE_DIR" && docker compose up -d "$BOT_SERVICE" ); }
    record_backoff_failure; exit 1
  fi
  # END-TO-END health check (liveness: process up + /health 200).
  ok=0; deadline=$(( $(now) + HEALTH_TIMEOUT ))
  while [ "$(now)" -lt "$deadline" ]; do curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1 && { ok=1; break; }; sleep 2; done
  if [ "$ok" != 1 ]; then
    err bot-unhealthy "review-bot failed health check after deploy; ROLLING BACK to :prev"
    if [ "$have_prev" = 1 ]; then docker tag "$BOT_IMAGE:prev" "$BOT_IMAGE:latest"; ( cd "$COMPOSE_DIR" && docker compose up -d "$BOT_SERVICE" ); fi
    record_backoff_failure; exit 1
  fi
  # blast-radius assertion: the gerrit container must be UNTOUCHED.
  gerrit_after="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"
  if [ -n "$gerrit_before" ] && [ "$gerrit_before" != "$gerrit_after" ]; then
    err blast-radius "gerrit container id changed during a review-bot deploy — investigate"
  fi
  prune_docker_caches
  log "review-bot redeployed + healthy"
fi

# ── host observability probe: re-materialize on a probe-source change ─────────
# The systemd timer executes /usr/local/bin/rebar-observability.sh, a COPY that ONLY
# install-observability.sh writes; nothing else refreshes it. infra/scripts/ is in no
# trigger above (not a BOT_PATH, so a probe-only change syncs nothing at all), so the
# installed copy would silently go stale (bug dying-verastile-quelea: 10 days stale).
# install-observability.sh is idempotent (re-copies the script, rewrites the unit files,
# daemon-reload), so re-running it from the TARGET source reconverges the host probe.
# Non-fatal: a probe-refresh failure must not roll back the review-bot, but it emits an
# AUTODEPLOY err marker so the staleness is alarmed instead of silent.
if changed "$OBS_PATHS"; then
  log "host observability probe sources changed; re-materializing from $TARGET"
  if ! git -C "$MIRROR_DIR" checkout -q "$TARGET" 2>/dev/null; then
    err obs-materialize-failed "git checkout $TARGET in $MIRROR_DIR failed; host probe left stale"
  elif ! bash "$MIRROR_DIR/infra/scripts/install-observability.sh"; then
    err obs-materialize-failed "install-observability.sh failed; /usr/local/bin probe may be stale"
  else
    log "host observability probe re-materialized on the box"
  fi
fi

# ── success: advance deployed-sha atomically, clear backoff ───────────────────
echo "$TARGET" > "$SHA_FILE.tmp" && mv "$SHA_FILE.tmp" "$SHA_FILE"
clear_backoff
log "deploy complete: env now reflects $TARGET"
