#!/usr/bin/env bash
# Reconcile the Gerrit box with the public origin/main mirror from a dedicated clone.
# Review-bot and MCP changes auto-apply independently; Gerrit/config changes are detect-only.
# A single-flight lock, bounded drain, candidate health checks, exact rollback, per-target
# backoff, and incomplete component markers prevent partial advancement of the fail-closed gate.
set -uo pipefail   # NOT -e: we handle failures explicitly (fail-safe, never half-updated)

# Tunables may be overridden through the environment file.
[ -f /etc/rebar/autodeploy.env ] && . /etc/rebar/autodeploy.env
DEPLOY_REPO="${DEPLOY_REPO:-/opt/rebar}"              # the compose build context (a COPY, not git)
COMPOSE_DIR="${COMPOSE_DIR:-$DEPLOY_REPO/infra/compose}"
MIRROR_DIR="${MIRROR_DIR:-/var/lib/rebar/mirror}"     # autodeploy's OWN regular git clone
MIRROR_URL="${MIRROR_URL:-https://github.com/navapbc/rebar.git}"   # PUBLIC mirror (read-only, HTTPS)
MIRROR_REMOTE="${MIRROR_REMOTE:-origin}"
STATE_DIR="${STATE_DIR:-/var/lib/rebar}"
LOCK="$STATE_DIR/deploy.lock"
SHA_FILE="$STATE_DIR/deployed-sha"
# MCP and review-bot completion markers track independent progress; absent markers fall
# back to the global deployed SHA for upgrade compatibility.
MCP_SHA_FILE="$STATE_DIR/mcp-deployed-sha"
# Record deploy order—not timestamps—so orphan cleanup preserves the exact previous release.
MCP_PREV_SHA_FILE="$STATE_DIR/mcp-previous-sha"
# Record review-bot completion before a later independent MCP failure can exit the tick.
BOT_SHA_FILE="$STATE_DIR/bot-deployed-sha"
BACKOFF_FILE="$STATE_DIR/deploy-backoff"              # "<target-sha> <fail-count> <next-epoch>"
# MCP backoff is separate so it cannot suppress a review-bot deploy.
MCP_BACKOFF_FILE="$STATE_DIR/mcp-deploy-backoff"
# Deferral records the first tick in the current episode, independent of target SHA.
DEFER_FILE="$STATE_DIR/deploy-defer"
BOT_SERVICE="${BOT_SERVICE:-review-bot}"              # compose service name (NEVER 'gerrit')
BOT_IMAGE="${BOT_IMAGE:-compose-review-bot}"
GERRIT_CONTAINER="${GERRIT_CONTAINER:-compose-gerrit-1}"
DECLARED_COMPOSE_SERVICES="gerrit review-bot opcert"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"   # review-bot receiver (NOT Gerrit 8080)
FETCH_TIMEOUT="${FETCH_TIMEOUT:-60}"                  # a hung fetch must not hold the lock
# Readiness must exceed the application's 60-second startup lock budget plus probe granularity.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
# Bound a drain episode above one review's 1200-second hard cap. Chronic pipelining may
# still exhaust it, at which point deployment proceeds and emits an interrupt marker.
DEPLOY_DEFER_MAX="${DEPLOY_DEFER_MAX:-2400}"
INFLIGHT_TIMEOUT="${INFLIGHT_TIMEOUT:-5}"             # bound the in-flight probe itself
HEALTH_FAIL_LOG_LINES="${HEALTH_FAIL_LOG_LINES:-100}"   # bounded stderr tail captured on bot-unhealthy
HEALTH_FAIL_LOG_BYTES="${HEALTH_FAIL_LOG_BYTES:-20000}" # …and a hard byte cap on that tail
BACKOFF_BASE="${BACKOFF_BASE:-60}"; BACKOFF_FACTOR="${BACKOFF_FACTOR:-2}"; BACKOFF_CAP="${BACKOFF_CAP:-900}"
# Read both daemon and on-demand Docker budgets from the shared cap script.
DOCKER_CAP_SH="${DOCKER_CAP_SH:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)/docker-storage-cap.sh}"
eval "$(bash "$DOCKER_CAP_SH" --print-env 2>/dev/null)" || true
# An unreadable budget skips capped pruning instead of inventing a second ceiling.
BUILD_CACHE_KEEP="${BUILD_CACHE_KEEP:-${DOCKER_BUILDKIT_CACHE_BYTES:-}}"
# Throttle root-pressure reclamation even on ticks with no deploy.
DISK_PRESSURE_PCT="${DISK_PRESSURE_PCT:-80}"
DISK_PRESSURE_HARD_PCT="${DISK_PRESSURE_HARD_PCT:-90}"
PRESSURE_PRUNE_MIN_INTERVAL="${PRESSURE_PRUNE_MIN_INTERVAL:-600}"
PRESSURE_PRUNE_TS_FILE="${PRESSURE_PRUNE_TS_FILE:-$STATE_DIR/pressure-prune-ts}"
# Persist consecutive ineffective reclaim cycles so monitoring distinguishes activity from recovery.
PRESSURE_STREAK_FILE="${PRESSURE_STREAK_FILE:-$STATE_DIR/pressure-prune-streak}"
PRESSURE_STREAK_ALARM="${PRESSURE_STREAK_ALARM:-3}"

# MCP deploys an immutable candidate beside the live backend, validates it, atomically
# flips nginx, then gracefully drains the old backend. Serving containers are never force-removed.
MCP_IMAGE="${MCP_IMAGE:-compose-mcp}"                 # `docker compose build mcp` image (project 'compose')
MCP_CONTAINER_PREFIX="${MCP_CONTAINER_PREFIX:-rebar-mcp}"     # autodeploy-managed container name prefix
MCP_COMPOSE_CONTAINER="${MCP_COMPOSE_CONTAINER:-compose-mcp-1}"  # the boot backend compose-up.sh brings up
MCP_UPSTREAM_FILE="${MCP_UPSTREAM_FILE:-/etc/nginx/mcp-upstream.conf}"  # materialized nginx /mcp/ include
MCP_PORT_A="${MCP_PORT_A:-8092}"; MCP_PORT_B="${MCP_PORT_B:-8093}"      # blue/green host ports (8091 reserved)
MCP_HEALTH_TIMEOUT="${MCP_HEALTH_TIMEOUT:-120}"       # readiness deadline for the NEW mcp container
# Refuse blue-green overlap below the memory floor; an unreadable probe fails open.
MCP_MEM_MIN_MB="${MCP_MEM_MIN_MB:-1024}"
# mechanism-ok: env_var MCP_MEM_LIMIT — story 48f0-f7ff-c8df-43ac: the live mcp container is
# created by docker run rather than compose, so the compose mem_limit must be mirrored here.
MCP_MEM_LIMIT="${MCP_MEM_LIMIT:-3712m}"
MCP_RELEASES_KEEP="${MCP_RELEASES_KEEP:-1}"           # retain the newest N mcp releases (the live one)
MCP_RELEASES_CAP="${MCP_RELEASES_CAP:-3}"             # hard cap on managed containers = the {8091,A,B} port pool
MCP_STOP_GRACE="${MCP_STOP_GRACE:-1260}"             # `docker stop --time`: >= _mcp_health grace (1200) + margin
# mechanism-ok: env_var MCP_SELF_HEAL — ticket 85a5 kill switch for the live-backend restart watchdog.
MCP_SELF_HEAL="${MCP_SELF_HEAL:-1}"

# Review-bot sources that trigger its independent redeploy.
BOT_PATHS='src/rebar/ infra/compose/Dockerfile.reviewbot pyproject.toml infra/compose/docker-compose.yml infra/scripts/reviewbot-ensure-tickets.sh'
# SSM materialization sources trigger both consumers; value-only rotation remains operator-driven.
SECRETS_PATHS='infra/scripts/fetch-secrets.sh infra/terraform/ssm.tf'
# MCP image sources include its baked entrypoint; shared sources independently trigger both services.
MCP_PATHS='src/rebar infra/compose/Dockerfile.mcp infra/scripts/mcp-entrypoint.sh infra/compose/docker-compose.yml uv.lock pyproject.toml'
# Gerrit configuration and materializers are detect-only because applying them touches or restarts Gerrit.
CONFIG_PATHS='infra/gerrit/replication.config infra/gerrit/project.config infra/gerrit/gerrit_to_platform.ini.template infra/gerrit/materialize-g2p-config.sh infra/gerrit/materialize-deploy-key.sh infra/compose/gerrit.config infra/compose/jgit.config infra/scripts/materialize-gerrit-jgit-config.sh'
# Host-nginx configuration is detect-only because applying it requires validation and reload.
EDGE_PATHS='infra/nginx/rebar.conf.template'
# Host-nginx materializers are detect-only; this loop never invokes compose-up or reloads nginx.
MATERIALIZER_PATHS='infra/scripts/compose-up.sh infra/scripts/materialize-opcert-guard.sh infra/scripts/materialize-mcp-upstream.sh infra/nginx/mcp-upstream.conf'
# Reinstall the host observability copy when its maintained sources change.
OBS_PATHS='infra/scripts/observability.sh infra/scripts/install-observability.sh'
# Reinstall the host certbot units when their source changes.
CERTBOT_PATHS='infra/scripts/install-certbot-timer.sh'
# Autodeploy never rewrites or reloads its own staged units while running; operators own
# that lifecycle. Rsync preserves materialized secrets, tokens, markers, and local state.
RSYNC_EXCLUDES=(--exclude '/.git' --exclude 'infra/compose/.env' \
  --exclude 'infra/compose/mcp-static-tokens.json' --exclude 'infra/compose/mcp-static-tokens.json.*' --exclude '/.deployed_ref' \
  --exclude '/.venv' --exclude '/.terraform' --exclude '/.serena' --exclude '/.claude' --exclude '/.tickets-tracker')

mkdir -p "$STATE_DIR"
now() { date +%s; }
log() { printf '{"event":"autodeploy","ts":%s,"msg":%s}\n' "$(now)" "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$*")"; }
# Marker tokens are stable alarm contracts; captured prose must not duplicate them.
marker() { printf '%s %s\n' "$1" "$(python3 -c 'import json,sys;print(json.dumps({"ts":int(sys.argv[1]),"reason":sys.argv[2],"detail":sys.argv[3]}))' "$(now)" "$2" "${3:-}")" >&2; }
err() { marker AUTODEPLOY_ERROR "$1" "${2:-}"; }

check_declared_compose_services() {
  local svc cid status failures
  [ -f "$COMPOSE_DIR/docker-compose.yml" ] || return 0
  failures=""
  for svc in $DECLARED_COMPOSE_SERVICES; do
    cid="$( cd "$COMPOSE_DIR" && docker compose ps -q "$svc" 2>/dev/null )" || cid=""
    if [ -z "$cid" ]; then
      failures="${failures}${svc}=missing;"
      continue
    fi
    status="$( docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null )" || status="unreadable"
    [ "$status" = "running" ] || failures="${failures}${svc}=${status};"
  done
  if [ -n "$failures" ]; then
    err declared-service-down "declared compose service not running: ${failures} decision=fail-loud-no-auto-restart"
    return 1
  fi
}

# Return current in-flight reviews, or -1 so an unreadable probe follows the fail-open path.
bot_in_flight_reviews() {
  local body count
  body="$(curl -fsS -m "$INFLIGHT_TIMEOUT" "$HEALTH_URL" 2>/dev/null)" || { echo -1; return 0; }
  count="$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    value = int(json.load(sys.stdin)["in_flight"])
except Exception:
    value = -1
print(value if value >= 0 else -1)
' 2>/dev/null)" || count=-1
  case "$count" in '' | *[!0-9-]*) count=-1 ;; esac
  echo "$count"
}

# Distinguish transient restart from a wedged bot. Unknown states return wedged so a broken
# probe cannot create an unbounded deferral; only redeploying enters the bounded defer path.
bot_unreachable_disposition() {
  local cid status health
  cid="$( cd "$COMPOSE_DIR" && docker compose ps -q "$BOT_SERVICE" 2>/dev/null )" || { echo wedged; return 0; }
  [ -n "$cid" ] || { echo wedged; return 0; }   # no container at all -> deploy to (re)create it
  status="$( docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null )" || { echo wedged; return 0; }
  case "$status" in
    restarting | created | removing | paused) echo redeploying; return 0 ;;
  esac
  # Only Docker's starting state proves a transient health gap.
  health="$( docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null )" || { echo wedged; return 0; }
  case "$health" in
    starting) echo redeploying; return 0 ;;
  esac
  echo wedged
}

# Capture candidate logs before rollback, bounded by time, lines, and bytes. Redact marker
# tokens so diagnostic prose cannot inflate observability counters; capture remains best-effort.
capture_bot_logs() {
  local out
  out="$( cd "$COMPOSE_DIR" && timeout 15 docker compose logs --no-color \
            --tail "$HEALTH_FAIL_LOG_LINES" "$BOT_SERVICE" 2>&1 \
          | tail -c "$HEALTH_FAIL_LOG_BYTES" )" \
    || out="(could not read $BOT_SERVICE logs)"
  [ -n "$out" ] || out="(no output from $BOT_SERVICE)"
  log "bot-unhealthy diagnostics — last $HEALTH_FAIL_LOG_LINES lines of $BOT_SERVICE: ${out//AUTODEPLOY_ERROR/AUTODEPLOY_ERR<redacted>}"
}

# Reclaim BuildKit cache and dangling images within bounded calls. Tagged rollback and MCP
# releases are untouched; failures stay non-fatal, and before/after free space records effect.
prune_docker_caches() {
  local before after pct hard
  before="$(root_disk_free_kb)"
  pct="$(root_disk_pct)"; case "$pct" in ''|*[!0-9]*) pct=0 ;; esac
  hard="$DISK_PRESSURE_HARD_PCT"; case "$hard" in ''|*[!0-9]*) hard=90 ;; esac
  if [ "$pct" -ge "$hard" ]; then
    log "prune_docker_caches: hard disk pressure (${pct}% >= ${hard}%): emergency builder prune without keep-storage"
    if ! timeout 120 docker builder prune -f >/dev/null 2>&1; then
      log "prune_docker_caches: builder prune failed (non-fatal)"
    fi
  elif [ -z "$BUILD_CACHE_KEEP" ]; then
    log "prune_docker_caches: BuildKit cap unavailable (docker-storage-cap.sh unreadable); skipping the capped builder prune rather than guessing a ceiling"
  elif ! timeout 120 docker builder prune -f --keep-storage "$BUILD_CACHE_KEEP" >/dev/null 2>&1; then
    log "prune_docker_caches: builder prune failed (non-fatal)"
  fi
  if ! timeout 120 docker image prune -f >/dev/null 2>&1; then
    log "prune_docker_caches: image prune failed (non-fatal)"
  fi
  after="$(root_disk_free_kb)"
  log "prune_docker_caches: root-disk free before=${before}kB after=${after}kB freed=$((after - before))kB"
  return 0
}

# Return available root kilobytes, defaulting unreadable output to zero.
root_disk_free_kb() {
  local kb
  kb="$(df --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')"
  echo "${kb:-0}"
}

# Return root used percent, defaulting unreadable output to zero.
root_disk_pct() {
  local pct
  pct="$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')"
  echo "${pct:-0}"
}

# Throttle no-op-tick reclamation and emit one marker for each triggered cycle.
reclaim_under_pressure() {
  local pct last after streak
  pct="$(root_disk_pct)"
  if [ "$pct" -lt "$DISK_PRESSURE_PCT" ]; then
    # Reset before returning from a recovered tick.
    if [ "$(read_pressure_streak)" -ne 0 ]; then
      write_pressure_streak 0
      log "disk pressure cleared ($pct% < $DISK_PRESSURE_PCT%): persistent-pressure streak reset"
    fi
    return 0
  fi
  last="$(cat "$PRESSURE_PRUNE_TS_FILE" 2>/dev/null || echo 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ $(( $(now) - last )) -lt "$PRESSURE_PRUNE_MIN_INTERVAL" ]; then
    log "disk pressure ($pct% >= $DISK_PRESSURE_PCT%) but throttled (last prune ${last}, interval ${PRESSURE_PRUNE_MIN_INTERVAL}s)"
    return 0
  fi
  marker AUTODEPLOY_DISK_PRESSURE "pressure-prune" "root disk at ${pct}% (threshold ${DISK_PRESSURE_PCT}%)"
  now > "$PRESSURE_PRUNE_TS_FILE.tmp" && mv "$PRESSURE_PRUNE_TS_FILE.tmp" "$PRESSURE_PRUNE_TS_FILE"
  log "disk pressure ($pct% >= $DISK_PRESSURE_PCT%): reclaiming docker garbage on the no-op tick"
  prune_docker_caches
  # Count ineffective reclaim cycles, not throttled ticks.
  after="$(root_disk_pct)"
  if [ "$after" -ge "$DISK_PRESSURE_PCT" ]; then
    streak=$(( $(read_pressure_streak) + 1 ))
    write_pressure_streak "$streak"
    if [ "$streak" -ge "$PRESSURE_STREAK_ALARM" ]; then
      marker AUTODEPLOY_DISK_PRESSURE_PERSISTS reclaim-ineffective \
        "root disk STILL ${after}% (threshold ${DISK_PRESSURE_PCT}%) after ${streak} consecutive reclaim cycles; reclaim is not recovering the disk"
    fi
  else
    streak=0
    write_pressure_streak 0
  fi
  log "disk pressure reclaim complete (root disk ${pct}% -> ${after}%; consecutive ineffective cycles ${streak}, alarm at ${PRESSURE_STREAK_ALARM})"
}

# Persist the streak atomically; unreadable values reset to zero rather than false-alarm.
read_pressure_streak() {
  local v
  v="$(cat "$PRESSURE_STREAK_FILE" 2>/dev/null || echo 0)"
  case "$v" in ''|*[!0-9]*) v=0 ;; esac
  echo "$v"
}
write_pressure_streak() {
  printf '%s\n' "$1" > "$PRESSURE_STREAK_FILE.tmp" \
    && mv "$PRESSURE_STREAK_FILE.tmp" "$PRESSURE_STREAK_FILE"
}

# MCP discovery and idempotent retirement are safe on no-op ticks.

# List managed MCP containers; -a includes stopped containers.
mcp_managed() {
  docker ps ${1:-} --format '{{.Names}}' 2>/dev/null \
    | grep -E "^(${MCP_CONTAINER_PREFIX}|${MCP_COMPOSE_CONTAINER})" || true
}
# The HOST port a managed container publishes container-port 8091 on (echoes nothing if unknown).
# `docker port` is fast for running containers, but an exited live backend can report no mapping
# there; fall back to HostConfig.PortBindings so the self-heal watchdog can map nginx's live port
# back to the stopped container identity it must restart in place (bug 85a5).
mcp_port_of() {
  local p
  p="$(docker port "$1" 8091/tcp 2>/dev/null | sed -E 's/.*:([0-9]+)$/\1/' | head -1)"
  if [ -n "$p" ]; then echo "$p"; return 0; fi
  docker inspect -f '{{json .HostConfig.PortBindings}}' "$1" 2>/dev/null \
    | sed -nE 's/.*"8091\/tcp":[^]]*"HostPort":"([0-9]+)".*/\1/p' | head -1
}
# The port the /mcp/ upstream include currently points at (the LIVE backend).
mcp_live_port() { sed -nE 's/.*server[[:space:]]+127\.0\.0\.1:([0-9]+);.*/\1/p' "$MCP_UPSTREAM_FILE" 2>/dev/null | head -1; }
# Return the image serving a known managed port, or nothing when unresolved.
mcp_image_on_port() {
  local want n
  want="$1"
  [ -n "$want" ] || return 0
  while read -r n; do
    [ -n "$n" ] || continue
    if [ "$(mcp_port_of "$n")" = "$want" ]; then
      docker inspect -f '{{.Config.Image}}' "$n" 2>/dev/null
      return 0
    fi
  done < <(mcp_managed)
}

# Retire only a reaped container's release image. Preserve build, latest, previous, and live
# references; non-forced removal adds Docker's own protection for any referenced image.
mcp_retire_image() {
  local img live_img
  img="$1"; live_img="$2"
  case "$img" in
    ''|"$MCP_IMAGE"|"$MCP_IMAGE:latest"|"$MCP_IMAGE:prev") return 0 ;;
  esac
  if [ -n "$live_img" ] && [ "$img" = "$live_img" ]; then
    return 0
  fi
  if docker image rm "$img" >/dev/null 2>&1; then
    log "mcp retire: removed image $img (its container was reaped; live image + build tag retained)"
  else
    log "mcp retire: image $img not removed (still referenced, or already gone) — non-fatal"
  fi
  return 0
}

# Enumerate release tags; rollback order comes from MCP_PREV_SHA_FILE, not this listing.
mcp_image_tags() {
  docker images "$MCP_IMAGE" --format '{{.Repository}}:{{.Tag}}' 2>/dev/null
}

# Sweep orphaned 40-hex release tags while preserving the live and recorded previous images.
# If either authoritative reference is unavailable, skip cleanup instead of guessing.
mcp_reconcile_orphans() {
  local live_ref prev_sha prev_ref ref tag removed=0 kept=0
  live_ref="$(mcp_image_on_port "$(mcp_live_port)")"
  [ -n "$live_ref" ] || { log "mcp reconcile: live image unknown; skipping orphan sweep (fail-safe)"; return 0; }
  prev_sha="$(cat "$MCP_PREV_SHA_FILE" 2>/dev/null | tr -d '[:space:]')"
  case "$prev_sha" in
    *[!0-9a-f]*|"") log "mcp reconcile: no recorded previous release yet; deferring orphan sweep until a deploy records one"; return 0 ;;
  esac
  [ "${#prev_sha}" -eq 40 ] || { log "mcp reconcile: recorded previous sha malformed; deferring orphan sweep"; return 0; }
  prev_ref="$MCP_IMAGE:$prev_sha"
  while read -r ref; do
    [ -n "$ref" ] || continue
    case "$ref" in "$MCP_IMAGE:"*) tag="${ref#"$MCP_IMAGE":}" ;; *) continue ;; esac
    case "$tag" in *[!0-9a-f]*|"") continue ;; esac   # only per-release <sha> tags (guard 1)
    [ "${#tag}" -eq 40 ] || continue
    { [ "$ref" = "$live_ref" ] || [ "$ref" = "$prev_ref" ]; } && { kept=$((kept + 1)); continue; }
    if docker image rm "$ref" >/dev/null 2>&1; then
      removed=$((removed + 1))
      log "mcp reconcile: retired orphan image $ref (no container; live=$live_ref prev=$prev_ref preserved)"
    else
      log "mcp reconcile: orphan $ref not removed (still referenced, or already gone) — non-fatal"
    fi
  done < <(mcp_image_tags)
  log "mcp reconcile: swept orphan mcp images (removed=$removed kept-live+prev=$kept)"
  return 0
}


# MiB available; -1 fails open.
mcp_mem_available_mb() {
  if [ -n "${MCP_MEM_AVAILABLE_MB:-}" ]; then
    case "$MCP_MEM_AVAILABLE_MB" in *[!0-9]*|'') echo -1 ;; *) echo "$MCP_MEM_AVAILABLE_MB" ;; esac
    return 0
  fi
  local kb
  kb="$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)"
  case "$kb" in ''|*[!0-9]*) echo -1; return 0 ;; esac
  echo $(( kb / 1024 ))
}

# Return a free blue-green port; no result makes the caller back off without collision.
mcp_free_port() {
  local bound="" n p
  while read -r n; do
    [ -n "$n" ] || continue
    p="$(mcp_port_of "$n")"; [ -n "$p" ] && bound="$bound $p"
  done < <(mcp_managed)
  for p in "$MCP_PORT_A" "$MCP_PORT_B"; do
    case " $bound " in *" $p "*) : ;; *) echo "$p"; return 0 ;; esac
  done
  return 0
}

# Start a blue-green container at compose parity. SSM-backed tickets/Jira values come only
# from --env-file to avoid empty shell overrides; the stable service label bounds metrics.
mcp_run_new() {
  docker run -d --name "$1" \
    --restart always \
    --label rebar.service=mcp \
    --memory "$MCP_MEM_LIMIT" \
    --stop-timeout "$MCP_STOP_GRACE" \
    --env-file "$COMPOSE_DIR/.env" \
    -e FORWARDED_ALLOW_IPS='*' \
    -e REBAR_MCP_TRANSPORT=http \
    -e REBAR_MCP_HTTP_HOST=0.0.0.0 \
    -e REBAR_MCP_HTTP_PORT=8091 \
    -e REBAR_MCP_HTTP_TLS_AT_EDGE=true \
    -e "REBAR_MCP_HTTP_ALLOWED_HOSTS=${REBAR_MCP_HTTP_ALLOWED_HOSTS:-rebar.solutions.navateam.com}" \
    -e "REBAR_MCP_HTTP_ALLOWED_ORIGINS=${REBAR_MCP_HTTP_ALLOWED_ORIGINS:-https://rebar.solutions.navateam.com}" \
    -e REBAR_MCP_AUTH_ENABLED=1 \
    -e REBAR_MCP_AUTH_STRATEGIES=static \
    -e "REBAR_MCP_AUTH_RESOURCE_SERVER_URL=${REBAR_MCP_AUTH_RESOURCE_SERVER_URL:-https://rebar.solutions.navateam.com/mcp}" \
    -e "REBAR_MCP_AUTH_ISSUER_URL=${REBAR_MCP_AUTH_ISSUER_URL:-https://rebar.solutions.navateam.com/mcp}" \
    -e REBAR_MCP_AUTH_STATIC_TOKENS_FILE=/run/secrets/mcp-static-tokens.json \
    -e REBAR_MCP_ALLOW_LLM=1 \
    -e "REBAR_OPCERT_ENV_ID=${REBAR_OPCERT_ENV_ID:-9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65}" \
    -e REBAR_IDENTITY_SIGNING_KEY=/run/secrets/opcert-ed25519-key \
    -e "REBAR_TRACKER_DIR=/var/gerrit/site/mcp-tickets" \
    -e "MCP_CODE_DIR=/var/gerrit/site/mcp-code" \
    -e "REBAR_ROOT=/var/gerrit/site/mcp-code" \
    -e REBAR_SYNC_PUSH=always \
    -e REBAR_LLM_BEDROCK_REGION=us-east-1 \
    -e AWS_DEFAULT_REGION=us-east-1 \
    -p "127.0.0.1:${2}:8091" \
    -v "$COMPOSE_DIR/mcp-static-tokens.json:/run/secrets/mcp-static-tokens.json:ro" \
    -v "$COMPOSE_DIR/opcert-ed25519-key:/run/secrets/opcert-ed25519-key:ro" \
    -v "gerrit_mcp_tickets:/var/gerrit/site/mcp-tickets" \
    -v "gerrit_mcp_code:/var/gerrit/site/mcp-code" \
    "$MCP_IMAGE:$TARGET" >/dev/null 2>&1
}

# Atomically replace the nginx include with a non-matching dotfile; validation or reload
# failure restores the previous include byte-for-byte.
mcp_flip_upstream() {
  local dir base tmp bak
  dir="$(dirname "$MCP_UPSTREAM_FILE")"; base="$(basename "$MCP_UPSTREAM_FILE")"
  tmp="$dir/.${base}.$$.tmp"; bak="$dir/.${base}.bak"
  cp "$MCP_UPSTREAM_FILE" "$bak" 2>/dev/null || true
  printf 'server 127.0.0.1:%s;\n' "$1" > "$tmp" || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$MCP_UPSTREAM_FILE" || { rm -f "$tmp"; return 1; }
  if ! nginx -t >/dev/null 2>&1 || ! nginx -s reload >/dev/null 2>&1; then
    [ -f "$bak" ] && mv "$bak" "$MCP_UPSTREAM_FILE"
    return 1
  fi
  rm -f "$bak"
  return 0
}

# Retire an old backend asynchronously with its bounded SIGTERM self-drain, never rm -f.
mcp_retire_graceful() {
  log "mcp retire: 'docker stop --time ${MCP_STOP_GRACE}' $1 in background (graceful SIGTERM self-drain; never rm -f a serving container)"
  # Do not let the background drain inherit the deploy lock.
  ( exec 9>&-; docker stop --time "$MCP_STOP_GRACE" "$1" >/dev/null 2>&1 ) &
}

# Drain old running backends and reap exited ones; cap overflow emits a marker, never force-kills.
mcp_retire_sweep() {
  local live live_img n p img count running
  live="$(mcp_live_port)"
  live_img="$(mcp_image_on_port "$live")"
  while read -r n; do
    [ -n "$n" ] || continue
    p="$(mcp_port_of "$n")"
    running=false
    [ "$(docker inspect -f '{{.State.Running}}' "$n" 2>/dev/null)" = "true" ] && running=true
    # Retain the named live backend regardless of container state so restart can recover it.
    if [ -n "$live" ] && [ "$p" = "$live" ]; then
      # Report a down live backend through the existing deploy-error alarm path.
      [ "$running" = true ] || err mcp-live-backend-down \
        "live mcp backend $n on port $p is NOT running; /mcp is failing. Retained (never reaped: nginx still points here); awaiting restart/redeploy"
      continue
    fi
    if [ "$running" = true ]; then
      # Unknown live-port state preserves all running containers; exited ones serve nothing.
      [ -z "$live" ] && continue
      mcp_retire_graceful "$n"
    else
      # Capture the image before rm and retire it only after a successful reap.
      img="$(docker inspect -f '{{.Config.Image}}' "$n" 2>/dev/null)"
      if docker rm "$n" >/dev/null 2>&1; then
        log "mcp retire: reaped exited $n (port ${p:-?} freed)"
        mcp_retire_image "$img" "$live_img"
      fi
    fi
  done < <(mcp_managed -a)
  count="$(mcp_managed -a | grep -c . || true)"
  case "$count" in ''|*[!0-9]*) count=0 ;; esac
  if [ "$count" -gt "$MCP_RELEASES_CAP" ]; then
    marker AUTODEPLOY_MCP_RETIRE_CAP over-cap "managed mcp containers=$count > cap=$MCP_RELEASES_CAP; NOT forcing a kill (containers still draining/holding ports)"
  fi
  # Sweep tags after reaping releases their container references.
  mcp_reconcile_orphans
}

# ── single-flight ─────────────────────────────────────────────────────────────
exec 9>"$LOCK"
flock -n 9 || { log "another deploy holds the lock; skipping"; exit 0; }

# Bootstrap an HTTPS-only mirror.
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

# Fetch origin/main within the lock's time budget.
if ! timeout "$FETCH_TIMEOUT" git -C "$MIRROR_DIR" fetch -q --prune "$MIRROR_REMOTE" main 2>/dev/null; then
  err fetch_failed "git fetch $MIRROR_REMOTE main timed out/failed (mirror may be stalling)"; exit 1
fi
TARGET="$(git -C "$MIRROR_DIR" rev-parse "$MIRROR_REMOTE/main")"
DEPLOYED="$(cat "$SHA_FILE" 2>/dev/null || true)"

# First run adopts an existing marker or the current tip without deploying.
if [ -z "$DEPLOYED" ]; then
  seed="$TARGET"
  if [ -f "$DEPLOY_REPO/.deployed_ref" ]; then
    ref="$(awk '{print $1}' "$DEPLOY_REPO/.deployed_ref" 2>/dev/null)"
    git -C "$MIRROR_DIR" rev-parse --verify -q "$ref^{commit}" >/dev/null 2>&1 && seed="$(git -C "$MIRROR_DIR" rev-parse "$ref")"
  fi
  echo "$seed" > "$SHA_FILE.tmp" && mv "$SHA_FILE.tmp" "$SHA_FILE"
  log "first run: adopting $seed as deployed-sha (no deploy)"; exit 0
fi
# Up to date: no deploy is pending, so any deferral episode is over. Clearing it here (and at
# the success footer) is what keeps a STALE episode from making the NEXT episode's bound look
# already-exhausted and killing a review on the first busy tick.
# backoff: same failed TARGET, not time yet -> skip. NEW target -> reset (fix-forward).
# This file records REVIEW-BOT failures (every record_backoff_failure call site is inside
# deploy_review_bot), so it gates the BOT path only. It used to `exit 0` the whole script,
# which — once the mcp path became independent — meant a bot failure suppressed mcp deploys
# on every subsequent tick: the exact coupling this change exists to remove, mirrored.
read -r bo_sha bo_cnt bo_next < <(cat "$BACKOFF_FILE" 2>/dev/null || echo "- 0 0")
[ "$bo_sha" != "$TARGET" ] && { bo_cnt=0; }   # new target -> reset (fix-forward)
bot_backoff_active() { [ "$bo_sha" = "$TARGET" ] && [ "$(now)" -lt "${bo_next:-0}" ]; }

record_backoff_failure() {
  local n=$(( ${bo_cnt:-0} + 1 ))
  local wait=$(( BACKOFF_BASE * (BACKOFF_FACTOR ** (n-1)) )); [ "$wait" -gt "$BACKOFF_CAP" ] && wait=$BACKOFF_CAP
  echo "$TARGET $n $(( $(now) + wait ))" > "$BACKOFF_FILE"
  err deploy_failed "target=$TARGET fail#$n backoff=${wait}s"
  log "deploy failed; backoff ${wait}s (fail #$n); last-known-good stays live"
  prune_docker_caches
}
clear_backoff() { rm -f "$BACKOFF_FILE"; }

# MCP backoff throttles only its own component.
read -r mcp_bo_sha mcp_bo_cnt mcp_bo_next < <(cat "$MCP_BACKOFF_FILE" 2>/dev/null || echo "- 0 0")
[ "$mcp_bo_sha" != "$TARGET" ] && { mcp_bo_cnt=0; }   # new target -> reset (fix-forward)
mcp_backoff_active() { [ "$mcp_bo_sha" = "$TARGET" ] && [ "$(now)" -lt "${mcp_bo_next:-0}" ]; }
# Schedule MCP throttling and cache cleanup; callers decide whether to emit an error marker.
schedule_mcp_backoff() {
  MCP_BACKOFF_N=$(( ${mcp_bo_cnt:-0} + 1 ))
  MCP_BACKOFF_WAIT=$(( BACKOFF_BASE * (BACKOFF_FACTOR ** (MCP_BACKOFF_N - 1)) ))
  [ "$MCP_BACKOFF_WAIT" -gt "$BACKOFF_CAP" ] && MCP_BACKOFF_WAIT=$BACKOFF_CAP
  echo "$TARGET $MCP_BACKOFF_N $(( $(now) + MCP_BACKOFF_WAIT ))" > "$MCP_BACKOFF_FILE"
  prune_docker_caches
}
record_mcp_backoff_failure() {
  schedule_mcp_backoff
  # Reuse deploy_failed and identify the component in its detail.
  err deploy_failed "component=mcp target=$TARGET fail#$MCP_BACKOFF_N backoff=${MCP_BACKOFF_WAIT}s"
  log "mcp deploy failed; mcp backoff ${MCP_BACKOFF_WAIT}s (fail #$MCP_BACKOFF_N); OLD mcp upstream stays live, review-bot path unaffected"
}
# Routine retire-cap and memory deferrals use dedicated metrics, not deploy_error.
record_mcp_routine_backoff() {
  schedule_mcp_backoff
  log "mcp deploy deferred (routine backoff; not a deploy_error); mcp backoff ${MCP_BACKOFF_WAIT}s (fail #$MCP_BACKOFF_N); OLD mcp upstream stays live, review-bot path unaffected"
}
clear_mcp_backoff() { rm -f "$MCP_BACKOFF_FILE"; }

mcp_health_contract_ok() {
  local port body store handshake
  port="$1"
  body="$(curl -fsS -m 3 "http://127.0.0.1:${port}/health" 2>/dev/null)" || return 1
  store="$(printf '%s' "$body" | python3 -c 'import json,sys
try:
    st = json.load(sys.stdin).get("store") or {}
except Exception:
    st = {}
print("missing" if st.get("expected") and not st.get("present") else "ok")' 2>/dev/null || echo ok)"
  [ "$store" = "missing" ] && return 1
  handshake="$(printf '%s' "$body" | python3 -c 'import json,sys
try:
    hs = json.load(sys.stdin).get("handshake") or {}
except Exception:
    hs = {}
print("failed" if hs.get("ok") is False else "ok")' 2>/dev/null || echo ok)"
  [ "$handshake" = "failed" ] && return 1
  return 0
}

mcp_wait_health_contract() {
  local port ok=0 deadline
  port="$1"; deadline=$(( $(now) + MCP_HEALTH_TIMEOUT ))
  while [ "$(now)" -lt "$deadline" ]; do
    mcp_health_contract_ok "$port" && { ok=1; break; }
    sleep 2
  done
  [ "$ok" = 1 ]
}

mcp_live_backend_name() {
  local live n p
  live="$(mcp_live_port)"
  [ -n "$live" ] || return 0
  while read -r n; do
    [ -n "$n" ] || continue
    p="$(mcp_port_of "$n")"
    [ "$p" = "$live" ] && { echo "$n"; return 0; }
  done < <(mcp_managed -a)
}

mcp_self_heal_live_backend() {
  local live name running
  live="$(mcp_live_port)"; [ -n "$live" ] || return 0
  name="$(mcp_live_backend_name)"; [ -n "$name" ] || return 0
  running=false
  [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ] && running=true
  [ "$running" = true ] && return 0
  if mcp_backoff_active; then
    log "mcp self-heal backoff active for $TARGET (fail #$mcp_bo_cnt); retaining dead live backend $name on port $live until $mcp_bo_next"
    return 0
  fi
  err mcp-live-backend-down "live mcp backend $name on port $live is NOT running; /mcp is failing"
  log "mcp self-heal: attempting in-place restart of $name on port $live"
  if [ "$MCP_SELF_HEAL" = "0" ]; then
    log "mcp self-heal disabled by MCP_SELF_HEAL=0; retaining dead live backend"
    return 0
  fi
  if ! docker start "$name" >/dev/null 2>&1; then
    record_mcp_backoff_failure
    return 1
  fi
  if ! mcp_wait_health_contract "$live"; then
    record_mcp_backoff_failure
    return 1
  fi
  clear_mcp_backoff
  log "mcp self-heal: restarted live backend $name on 127.0.0.1:${live}; nginx upstream unchanged"
  return 0
}

# ── what changed? (computed in the mirror clone) ──────────────────────────────
changed_range() { git -C "$MIRROR_DIR" diff --name-only "$1" "$2" -- $3 2>/dev/null | grep -q .; }
changed() { changed_range "$DEPLOYED" "$TARGET" "$1"; }
# MCP redeploys for either image-source or startup-secret changes.
mcp_delta() {
  changed_range "$mcp_deployed" "$TARGET" "$MCP_PATHS" || \
    changed_range "$mcp_deployed" "$TARGET" "$SECRETS_PATHS"
}

# Invalid component markers fall back to the global deployed SHA.
mcp_deployed="$(cat "$MCP_SHA_FILE" 2>/dev/null || true)"
if [ -z "$mcp_deployed" ] || ! git -C "$MIRROR_DIR" rev-parse --verify -q "$mcp_deployed^{commit}" >/dev/null 2>&1; then
  mcp_deployed="$DEPLOYED"
fi
bot_deployed="$(cat "$BOT_SHA_FILE" 2>/dev/null || true)"
if [ -z "$bot_deployed" ] || ! git -C "$MIRROR_DIR" rev-parse --verify -q "$bot_deployed^{commit}" >/dev/null 2>&1; then
  bot_deployed="$DEPLOYED"
fi

# Up to date: no deploy is pending, so any deferral episode is over. Clearing it here (and at
# the success footer) is what keeps a STALE episode from making the NEXT episode's bound look
# already-exhausted and killing a review on the first busy tick. The MCP self-heal runs before
# the ordinary no-op exit so a dead live backend does not stay pinned behind nginx until a future
# source-changing deploy.
if [ "$TARGET" = "$DEPLOYED" ]; then
  rm -f "$DEFER_FILE"
  reclaim_under_pressure
  mcp_self_heal_live_backend || exit 1
  mcp_retire_sweep          # reap mcp containers that finished draining since the last flip
  check_declared_compose_services || exit 1
  log "up to date ($TARGET); no-op"
  exit 0
fi

log "main advanced $DEPLOYED -> $TARGET; computing component deltas"
mcp_self_heal_live_backend || exit 1

# Signal Gerrit configuration changes for manual application.
if changed "$CONFIG_PATHS"; then
  err config_manual "infra config changed in $TARGET — replication/g2p/refs-meta/gerrit.config/jgit.config need a MANUAL operator apply (auto-apply is a v2 follow-up)"
  log "infra config change detected + signalled (not auto-applied in v1)"
fi

# Flag edge changes for validated reload.
if changed "$EDGE_PATHS"; then
  err nginx_edge_manual "nginx edge changed in $TARGET — infra/nginx/rebar.conf.template needs a MANUAL operator render + nginx reload — see infra/runbooks/nginx-edge-render.md (auto-apply is a v2 follow-up: epic 6d60-2d0c-6ff7-444b)"
  log "nginx edge change detected + signalled (not auto-applied in v1)"
fi

# Flag materializers for manual application.
if changed "$MATERIALIZER_PATHS"; then
  err nginx_materializer_manual "host-nginx materializer source changed in $TARGET — compose-up.sh / materialize-opcert-guard.sh / materialize-mcp-upstream.sh / mcp-upstream.conf need a MANUAL operator re-materialize + nginx reload (auto-apply is a v2 follow-up: epic 6d60-2d0c-6ff7-444b)"
  log "host-nginx materializer change detected + signalled (not auto-applied in v1)"
fi

# Keep review-bot deployment in a function so deferral returns to the independent MCP path.
bot_deferred=0
# Pending deltas prevent the completion footer from stamping an undeployed target.
bot_incomplete=0
# MCP uses the same incomplete-tick guard.
mcp_incomplete=0
deploy_review_bot() {
  # Defer container recreation while reviews are active, coalescing changes without holding
  # the deploy lock between ticks. The episode bound prevents permanent starvation.
  inflight="$(bot_in_flight_reviews)"
  defer_since="$(cat "$DEFER_FILE" 2>/dev/null || echo 0)"
  case "$defer_since" in '' | *[!0-9]*) defer_since=0 ;; esac
  if [ "$inflight" -gt 0 ]; then
    # Bound the continuous busy episode, not its changing target SHA.
    [ "$defer_since" -eq 0 ] && { defer_since="$(now)"; echo "$defer_since" >"$DEFER_FILE"; }
    waited=$(( $(now) - defer_since ))
    if [ "$waited" -lt "$DEPLOY_DEFER_MAX" ]; then
      marker AUTODEPLOY_DEFERRED review-in-flight \
        "target=$TARGET in_flight=$inflight deferred_for=${waited}s bound=${DEPLOY_DEFER_MAX}s"
      log "review-bot busy ($inflight review(s) in flight); DEFERRING the deploy of $TARGET (${waited}s of the ${DEPLOY_DEFER_MAX}s bound used); deployed-sha unchanged; retrying on the next timer tick"
      # Preserve the episode and return to the independent MCP path without advancing markers.
      bot_deferred=1
      return 0
    fi
    # A bounded interruption is countable before recreation proceeds.
    marker AUTODEPLOY_REVIEW_INTERRUPT bound-exceeded \
      "target=$TARGET in_flight=$inflight deferred_for=${waited}s bound=${DEPLOY_DEFER_MAX}s; recreating anyway, so an in-flight review IS being killed"
    log "deferral bound ${DEPLOY_DEFER_MAX}s exhausted with $inflight review(s) still in flight; proceeding (a review is interrupted; the backfill reconciler retries it)"
  elif [ "$inflight" -lt 0 ]; then
    # Defer a bot still starting; treat other unreadable health as wedged.
    disposition="$(bot_unreachable_disposition)"
    if [ "$disposition" = redeploying ]; then
      # Reuse the busy episode bound and a distinct redeploying reason.
      [ "$defer_since" -eq 0 ] && { defer_since="$(now)"; echo "$defer_since" >"$DEFER_FILE"; }
      waited=$(( $(now) - defer_since ))
      if [ "$waited" -lt "$DEPLOY_DEFER_MAX" ]; then
        marker AUTODEPLOY_DEFERRED bot-redeploying \
          "target=$TARGET; /health unreadable while the bot is mid-redeploy — deferring rather than recreating it blind (${waited}s of the ${DEPLOY_DEFER_MAX}s bound used)"
        log "review-bot /health unreadable but the bot is mid-redeploy; DEFERRING the deploy of $TARGET (${waited}s of the ${DEPLOY_DEFER_MAX}s bound used); deployed-sha unchanged; retrying on the next timer tick"
        bot_deferred=1
        return 0
      fi
      log "mid-redeploy deferral bound ${DEPLOY_DEFER_MAX}s exhausted with /health still unreadable; proceeding via the fail-open recreate below"
    fi
    # A wedged bot fails open toward repair, with an interrupt marker for the blind deploy.
    marker AUTODEPLOY_REVIEW_INTERRUPT signal-unavailable \
      "target=$TARGET; /health in_flight unreadable at $HEALTH_URL — deploying WITHOUT a drain check, so a review may be killed unobserved"
    log "in-flight review signal unavailable at $HEALTH_URL; proceeding without the drain check (fail-open: a broken bot is fixed BY deploying)"
  fi
  rm -f "$DEFER_FILE"
  log "review-bot sources or secrets changed; sync + refresh .env + rebuild + restart (blast radius = $BOT_SERVICE only)"
  # Sync target source from the mirror into the copy-based build context.
  if ! git -C "$MIRROR_DIR" checkout -q "$TARGET" 2>/dev/null; then
    err mirror-checkout-failed "git checkout $TARGET in $MIRROR_DIR failed"; record_backoff_failure; exit 1
  fi
  if ! rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$MIRROR_DIR/" "$DEPLOY_REPO/" 2>/dev/null; then
    err rsync-failed "rsync $MIRROR_DIR -> $DEPLOY_REPO failed"; record_backoff_failure; exit 1
  fi
  # Preserve the excluded secret file's owner while normalizing the source copy.
  env_owner="$(stat -c '%U:%G' "$DEPLOY_REPO/infra/compose/.env" 2>/dev/null || true)"
  chown -R 502:502 "$DEPLOY_REPO" 2>/dev/null || true
  [ -n "$env_owner" ] && chown "$env_owner" "$DEPLOY_REPO/infra/compose/.env" 2>/dev/null || true

  # Refresh excluded SSM secrets before tagging or building; failure leaves the running image intact.
  if ! ENV_FILE="$DEPLOY_REPO/infra/compose/.env" bash "$DEPLOY_REPO/infra/scripts/fetch-secrets.sh" >/dev/null 2>&1; then
    err secrets-fetch-failed "fetch-secrets.sh failed (SSM unreachable / param missing); .env left intact; deploy aborted (bot stays on current image)"
    record_backoff_failure; exit 1
  fi
  # Reassert the preserved owner after the atomic 0600 rewrite.
  [ -n "$env_owner" ] && chown "$env_owner" "$DEPLOY_REPO/infra/compose/.env" 2>/dev/null || true

  gerrit_before="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"
  # Preserve the current image for exact rollback.
  if docker image inspect "$BOT_IMAGE:latest" >/dev/null 2>&1; then docker tag "$BOT_IMAGE:latest" "$BOT_IMAGE:prev"; have_prev=1; else have_prev=0; fi
  if ! ( cd "$COMPOSE_DIR" && docker compose build "$BOT_SERVICE" && docker compose up -d "$BOT_SERVICE" ); then
    err bot-build-failed "compose build/up $BOT_SERVICE failed"
    [ "$have_prev" = 1 ] && { docker tag "$BOT_IMAGE:prev" "$BOT_IMAGE:latest"; ( cd "$COMPOSE_DIR" && docker compose up -d "$BOT_SERVICE" ); }
    record_backoff_failure; exit 1
  fi
  # Require candidate /health before success.
  ok=0; deadline=$(( $(now) + HEALTH_TIMEOUT ))
  while [ "$(now)" -lt "$deadline" ]; do curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1 && { ok=1; break; }; sleep 2; done
  if [ "$ok" != 1 ]; then
    # Capture candidate evidence before rollback replaces it.
    capture_bot_logs
    err bot-unhealthy "review-bot failed health check within ${HEALTH_TIMEOUT}s after deploy; ROLLING BACK to :prev (container log tail captured above)"
    if [ "$have_prev" = 1 ]; then docker tag "$BOT_IMAGE:prev" "$BOT_IMAGE:latest"; ( cd "$COMPOSE_DIR" && docker compose up -d "$BOT_SERVICE" ); fi
    record_backoff_failure; exit 1
  fi
  # Assert the Gerrit container was untouched.
  gerrit_after="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"
  if [ -n "$gerrit_before" ] && [ "$gerrit_before" != "$gerrit_after" ]; then
    err blast-radius "gerrit container id changed during a review-bot deploy — investigate"
  fi
  # Record bot completion before any later MCP failure can abort the tick.
  echo "$TARGET" > "$BOT_SHA_FILE.tmp" && mv "$BOT_SHA_FILE.tmp" "$BOT_SHA_FILE"
  bot_deployed="$TARGET"
  prune_docker_caches
  log "review-bot redeployed + healthy"
}
# Test the delta before backoff so pending work always sets the incomplete marker.
if changed_range "$bot_deployed" "$TARGET" "$BOT_PATHS" || \
   changed_range "$bot_deployed" "$TARGET" "$SECRETS_PATHS"; then
  if bot_backoff_active; then
    log "review-bot backoff active for $TARGET (fail #$bo_cnt); next bot attempt at $bo_next; the bot delta is still PENDING so deployed-sha will NOT advance"
    bot_incomplete=1
  else
    deploy_review_bot
  fi
fi

# Deploy MCP independently by immutable candidate, health checks, atomic nginx flip, and
# graceful old-backend retirement. Failure leaves the old upstream and Gerrit untouched.
if mcp_backoff_active && mcp_delta; then
  log "mcp backoff active for $TARGET (fail #$mcp_bo_cnt); next mcp attempt at $mcp_bo_next; the mcp delta is still PENDING so deployed-sha will NOT advance"
  mcp_incomplete=1
fi
if ! mcp_backoff_active && mcp_delta; then
  log "mcp sources changed $mcp_deployed -> $TARGET; blue-green deploy (blast radius = mcp containers + /mcp nginx upstream only)"
  gerrit_before_mcp="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"

  # 1. Sync and tag the immutable candidate image.
  if ! git -C "$MIRROR_DIR" checkout -q "$TARGET" 2>/dev/null; then
    err mcp-checkout-failed "git checkout $TARGET in $MIRROR_DIR failed"; record_mcp_backoff_failure; exit 1
  fi
  if ! rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$MIRROR_DIR/" "$DEPLOY_REPO/" 2>/dev/null; then
    err mcp-rsync-failed "rsync $MIRROR_DIR -> $DEPLOY_REPO failed"; record_mcp_backoff_failure; exit 1
  fi
  # Refresh both rsync-excluded SSM artifacts before building; failure preserves the live upstream.
  if ! ENV_FILE="$COMPOSE_DIR/.env" bash "$DEPLOY_REPO/infra/scripts/fetch-secrets.sh" >/dev/null 2>&1; then
    err mcp-secrets-fetch-failed "fetch-secrets.sh failed (SSM unreachable / param missing); .env left intact; mcp deploy aborted (old container stays live)"
    record_mcp_backoff_failure; exit 1
  fi
  if ! ( cd "$COMPOSE_DIR" && docker compose build mcp ); then
    err mcp-build-failed "docker compose build mcp failed"; record_mcp_backoff_failure; exit 1
  fi
  if ! docker tag "$MCP_IMAGE" "$MCP_IMAGE:$TARGET" >/dev/null 2>&1; then
    err mcp-tag-failed "docker tag $MCP_IMAGE -> $MCP_IMAGE:$TARGET failed"; record_mcp_backoff_failure; exit 1
  fi

  # 2. Check memory before overlap; unreadable memory fails open.
  mcp_mem="$(mcp_mem_available_mb)"
  if [ "$mcp_mem" -ge 0 ] && [ "$mcp_mem" -lt "$MCP_MEM_MIN_MB" ]; then
    marker AUTODEPLOY_MCP_MEM_ABORT low-memory "MemAvailable=${mcp_mem}MB < min ${MCP_MEM_MIN_MB}MB on the 8GiB box; refusing the blue-green 2x overlap"
    record_mcp_routine_backoff; exit 1
  fi
  [ "$mcp_mem" -lt 0 ] && log "mcp mem-check: MemAvailable UNREADABLE; failing OPEN (proceeding with the 2x overlap without a memory guarantee)"

  # 3. Require a free managed port; exhaustion emits a cap marker and starts nothing.
  mcp_newport="$(mcp_free_port)"
  if [ -z "$mcp_newport" ]; then
    marker AUTODEPLOY_MCP_RETIRE_CAP port-exhausted "both $MCP_PORT_A and $MCP_PORT_B held by un-reaped mcp containers; not starting a colliding 3rd (cap=$MCP_RELEASES_CAP)"
    record_mcp_routine_backoff; exit 1
  fi
  mcp_newname="${MCP_CONTAINER_PREFIX}-${TARGET:0:12}-${mcp_newport}"

  # 4. Start the parallel candidate.
  if ! mcp_run_new "$mcp_newname" "$mcp_newport"; then
    err mcp-run-failed "docker run $mcp_newname on 127.0.0.1:${mcp_newport} failed"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    record_mcp_backoff_failure; exit 1
  fi

  # 5. Remove an unhealthy candidate without changing the old upstream.
  mcp_ok=0; mcp_deadline=$(( $(now) + MCP_HEALTH_TIMEOUT ))
  while [ "$(now)" -lt "$mcp_deadline" ]; do
    curl -fsS -m 3 "http://127.0.0.1:${mcp_newport}/health" >/dev/null 2>&1 && { mcp_ok=1; break; }
    sleep 2
  done
  if [ "$mcp_ok" != 1 ]; then
    err mcp-unhealthy "new mcp container $mcp_newname failed /health within ${MCP_HEALTH_TIMEOUT}s; removing it, leaving the OLD upstream live"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    record_mcp_backoff_failure; exit 1
  fi

  # 5b. When health says a store is expected, require it; absent fields keep mixed versions compatible.
  mcp_store="$(curl -fsS -m 3 "http://127.0.0.1:${mcp_newport}/health" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    st = json.load(sys.stdin).get("store") or {}
except Exception:
    st = {}
print("missing" if st.get("expected") and not st.get("present") else "ok")' 2>/dev/null || echo ok)"
  if [ "$mcp_store" = "missing" ]; then
    err mcp-store-missing "new mcp container $mcp_newname is healthy but reports NO ticket store while one is configured; removing it, leaving the OLD upstream live"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    # Record only MCP backoff so review-bot remains independent.
    record_mcp_backoff_failure; exit 1
  fi

  # 5c. Require a reported startup MCP handshake; absent fields keep mixed versions compatible.
  mcp_handshake="$(curl -fsS -m 3 "http://127.0.0.1:${mcp_newport}/health" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    hs = json.load(sys.stdin).get("handshake") or {}
except Exception:
    hs = {}
print("failed" if hs.get("ok") is False else "ok")' 2>/dev/null || echo ok)"
  if [ "$mcp_handshake" = "failed" ]; then
    err mcp-handshake-failed "new mcp container $mcp_newname answers /health but reports its startup MCP handshake FAILED, so it cannot serve an MCP request; removing it, leaving the OLD upstream live"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    record_mcp_backoff_failure; exit 1
  fi


  # 6. Flip atomically; failure restores the exact include and removes the candidate.
  if ! mcp_flip_upstream "$mcp_newport"; then
    err mcp-flip-failed "nginx flip to 127.0.0.1:${mcp_newport} failed; restored previous upstream + removing new container"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    record_mcp_backoff_failure; exit 1
  fi
  log "mcp cutover complete: /mcp upstream now 127.0.0.1:${mcp_newport} (deploy DONE; not waiting on in-flight drain)"

  # Record the outgoing SHA before retirement so cleanup preserves the rollback target.
  case "$mcp_deployed" in
    *[!0-9a-f]*|"") : ;;
    *) if [ "${#mcp_deployed}" -eq 40 ] && [ "$mcp_deployed" != "$TARGET" ]; then
         echo "$mcp_deployed" > "$MCP_PREV_SHA_FILE.tmp" && mv "$MCP_PREV_SHA_FILE.tmp" "$MCP_PREV_SHA_FILE"
         log "mcp reconcile: recorded previous release $mcp_deployed (rollback target; preserved by orphan sweep)"
       fi ;;
  esac

  # 7. Retire the old backend outside the cutover path.
  mcp_retire_sweep

  # Assert the Gerrit container was untouched.
  gerrit_after_mcp="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"
  if [ -n "$gerrit_before_mcp" ] && [ "$gerrit_before_mcp" != "$gerrit_after_mcp" ]; then
    err blast-radius "gerrit container id changed during an mcp deploy — investigate"
  fi
  # Record MCP completion before an incomplete bot path can exit the tick.
  echo "$TARGET" > "$MCP_SHA_FILE.tmp" && mv "$MCP_SHA_FILE.tmp" "$MCP_SHA_FILE"
  mcp_deployed="$TARGET"
  clear_mcp_backoff
  prune_docker_caches
  log "mcp redeployed + healthy at 127.0.0.1:${mcp_newport}"
fi

# Incomplete components block global advancement after both independent paths had their turn.
if [ "$bot_deferred" = 1 ] || [ "$bot_incomplete" = 1 ] || [ "$mcp_incomplete" = 1 ]; then
  exit 0
fi

# Reinstall the host observability probe non-fatally when its source changes.
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

# Reinstall host certbot units non-fatally when their source changes.
if changed "$CERTBOT_PATHS"; then
  log "host certbot renew timer sources changed; re-materializing from $TARGET"
  if ! git -C "$MIRROR_DIR" checkout -q "$TARGET" 2>/dev/null; then
    err certbot-materialize-failed "git checkout $TARGET in $MIRROR_DIR failed; host certbot timer left stale"
  elif ! bash "$MIRROR_DIR/infra/scripts/install-certbot-timer.sh"; then
    err certbot-materialize-failed "install-certbot-timer.sh failed; /etc/systemd/system certbot-renew units may be stale"
  else
    log "host certbot renew timer re-materialized on the box"
  fi
fi

# Advance after all components complete.
echo "$TARGET" > "$SHA_FILE.tmp" && mv "$SHA_FILE.tmp" "$SHA_FILE"
# Keep component markers in lockstep with a completed global target.
echo "$TARGET" > "$MCP_SHA_FILE.tmp" && mv "$MCP_SHA_FILE.tmp" "$MCP_SHA_FILE"
echo "$TARGET" > "$BOT_SHA_FILE.tmp" && mv "$BOT_SHA_FILE.tmp" "$BOT_SHA_FILE"
clear_backoff
clear_mcp_backoff
rm -f "$DEFER_FILE"          # the deferral episode ended with a deploy; do not carry it forward
log "deploy complete: env now reflects $TARGET"
