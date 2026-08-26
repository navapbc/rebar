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
#   - drain gate: a recreation that would KILL an in-flight review is DEFERRED to the next
#     timer tick (bounded by DEPLOY_DEFER_MAX, then it proceeds and emits a countable
#     AUTODEPLOY_REVIEW_INTERRUPT marker). Without it a landing burst live-locks the gate
#     while every health signal reads green — a killed review fails nothing, so it emits no
#     VOTER_ERROR and leaves restarts=0 (bug 34cd).
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
# Deferral episode marker: "<epoch of the FIRST tick that deferred>". Deliberately NOT keyed
# to the target SHA the way BACKOFF_FILE is — see the drain gate below for why.
DEFER_FILE="$STATE_DIR/deploy-defer"
BOT_SERVICE="${BOT_SERVICE:-review-bot}"              # compose service name (NEVER 'gerrit')
BOT_IMAGE="${BOT_IMAGE:-compose-review-bot}"
GERRIT_CONTAINER="${GERRIT_CONTAINER:-compose-gerrit-1}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"   # review-bot receiver (NOT Gerrit 8080)
FETCH_TIMEOUT="${FETCH_TIMEOUT:-60}"                  # a hung fetch must not hold the lock
# Readiness deadline for the freshly-deployed review-bot. It MUST outlast the budgets the
# APPLICATION itself may legitimately spend on a cold start, or the deploy rolls back a
# container that was only slow. The dominant term is the store write lock taken by
# `run_ensures()` on the bot's startup path: src/rebar/_store/lock.py `_DEFAULT_TIMEOUT` (30)
# x `_DEFAULT_ATTEMPTS` (2) = a 60s wait before it logs and continues. The readiness loop below
# adds up to 5s of granularity (sleep 2 + curl -m 3), so ~65s is the floor and the 30s this
# defaulted to sat below even the lock budget alone — on 2026-07-31 a ~62s cold start was killed
# at +30s and rolled back seven consecutive times, each rolled-back container then becoming
# healthy ~30s later. 120s is 2x the dominant internal budget, leaving headroom for a cold/slow
# box. A genuinely broken image is still rolled back — just 90s later — and the timer plus the
# SHA-keyed backoff still bound the retry rate. Keep this ABOVE the app's own budgets if either
# side changes (tests/scripts/test_autodeploy_health_gate.py asserts the relationship).
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
# Drain-gate bound (bug 34cd): the longest a deferral EPISODE may hold a deploy back before
# we recreate the container anyway. It MUST exceed one whole review, or we would force-deploy
# through a review that was about to finish and gain nothing. The review-bot's own hard cap on
# a single review is REVIEW_TIMEOUT_SECONDS (src/rebar/review_bot/config.py
# DEFAULT_REVIEW_TIMEOUT_SECONDS = 1200): the worker and the backfill reconciler both wrap
# review_and_vote in asyncio.wait_for at that value, so NO single review can outlive it —
# in-flight necessarily falls to 0 within 1200s of the last review starting. 2400s is 2.0x that
# ceiling (and ~4x the ~10-minute review measured on 2026-08-03), so the bound can only be
# reached by a bot that is CHRONICALLY busy — a standing queue of reviews — never by one
# ordinary review. Keep this ABOVE the app's per-review cap if either side changes
# (tests/scripts/test_autodeploy_review_drain.py asserts the relationship).
#
# RAISED 1800s -> 2400s (1.5x -> 2.0x) on 2026-08-12. 34cd's original 1.5x was marginally
# undersized for real review durations: ALL FOUR bound-exceeded interrupts that day cleared the
# bound by a hair — deferred_for 1810s / 1820s / 1810s / 1930s against bound=1800s, i.e. 10s, 20s,
# 10s and 130s over — during a 60+-merge landing burst that ran the interrupt rate ~10x above the
# 11-day baseline. Each was a review that had finished, or was about to, when the budget expired.
# 2.0x clears the worst of them (1930s) by 470s, ~3.6x the largest overshoot.
#
# What this does NOT fix, deliberately: bug 7b4a traced a deferral episode where `in_flight`
# oscillated 2 -> 1 -> 2 and NEVER reached 0 across 30 continuous minutes. No single review
# outlived REVIEW_TIMEOUT_SECONDS there; the bound was outlasted by ordinary PIPELINING, because
# in_flight is a concurrent GAUGE and this bound is reasoned about as if it were a single-review
# timer. Against a saturated episode a bigger bound only defers the same kill — which is why the
# raise stays a fixed MULTIPLE of the app's own cap rather than an open-ended increase: a deploy
# that never lands is its own outage. Fixing saturation needs a drain signal that distinguishes
# "a review is finishing" from "the queue is refilling", and is tracked on 7b4a's chain.
DEPLOY_DEFER_MAX="${DEPLOY_DEFER_MAX:-2400}"
INFLIGHT_TIMEOUT="${INFLIGHT_TIMEOUT:-5}"             # bound the in-flight probe itself
HEALTH_FAIL_LOG_LINES="${HEALTH_FAIL_LOG_LINES:-100}"   # bounded stderr tail captured on bot-unhealthy
HEALTH_FAIL_LOG_BYTES="${HEALTH_FAIL_LOG_BYTES:-20000}" # …and a hard byte cap on that tail
BACKOFF_BASE="${BACKOFF_BASE:-60}"; BACKOFF_FACTOR="${BACKOFF_FACTOR:-2}"; BACKOFF_CAP="${BACKOFF_CAP:-900}"
BUILD_CACHE_KEEP="${BUILD_CACHE_KEEP:-5GB}"           # buildkit cache hard cap (docker builder prune --keep-storage)
# Disk-pressure reclaim on the no-op path (incident 2731 follow-up, story 28f9): a quiescent
# `main` never hits the deploy/backoff paths where prune_docker_caches already runs, so a
# build burst could fill the root disk and stay pinned until the NEXT real deploy (~39h
# observed). DISK_PRESSURE_PCT is the root-disk used-% threshold (matches observability.sh's
# root-disk pressure alarm framing); PRESSURE_PRUNE_MIN_INTERVAL throttles how often the no-op
# path may reclaim; PRESSURE_PRUNE_TS_FILE persists the last-reclaim timestamp across ticks.
DISK_PRESSURE_PCT="${DISK_PRESSURE_PCT:-80}"
PRESSURE_PRUNE_MIN_INTERVAL="${PRESSURE_PRUNE_MIN_INTERVAL:-600}"
PRESSURE_PRUNE_TS_FILE="${PRESSURE_PRUNE_TS_FILE:-$STATE_DIR/pressure-prune-ts}"

# ── mcp blue-green target (panicky-sylphish-foxterrier / ADR 0079 amendment / 0104) ──
# The on-box rebar-mcp server is a NEVER-IDLE shared endpoint, so the review-bot's
# stop-and-health-drain deploy above cannot be reused: its gauge-gated drain bound can never
# reach zero (review-bot bug 7b4a). Instead this target models the LOCAL origin/main updater —
# immutable release + ATOMIC nginx pointer swap + retire-when-idle: build+tag a new image, start
# a NEW container ALONGSIDE the old on a free blue/green port, health-check it, atomically flip
# the /mcp/ upstream include to it (deploy DONE here — it never waits on an in-flight op), then
# retire the OLD container off the critical path with a GRACEFUL `docker stop` (SIGTERM triggers
# the container's OWN bounded self-drain, _mcp_health.run_http_with_grace) — NEVER `docker rm -f`
# a serving container. Managed HOST ports are EXACTLY {8091 (compose-original), MCP_PORT_A,
# MCP_PORT_B}; the container port is always 8091.
MCP_IMAGE="${MCP_IMAGE:-compose-mcp}"                 # `docker compose build mcp` image (project 'compose')
MCP_CONTAINER_PREFIX="${MCP_CONTAINER_PREFIX:-rebar-mcp}"     # autodeploy-managed container name prefix
MCP_COMPOSE_CONTAINER="${MCP_COMPOSE_CONTAINER:-compose-mcp-1}"  # the boot backend compose-up.sh brings up
MCP_UPSTREAM_FILE="${MCP_UPSTREAM_FILE:-/etc/nginx/mcp-upstream.conf}"  # materialized nginx /mcp/ include
MCP_PORT_A="${MCP_PORT_A:-8092}"; MCP_PORT_B="${MCP_PORT_B:-8093}"      # blue/green host ports (8091 reserved)
MCP_HEALTH_TIMEOUT="${MCP_HEALTH_TIMEOUT:-120}"       # readiness deadline for the NEW mcp container
# 8 GiB t4g.large: a blue-green overlap briefly DOUBLES the mcp footprint, so refuse the second
# container when MemAvailable is below this floor. Unreadable fails OPEN (a broken probe must not
# wedge deploys); MCP_MEM_AVAILABLE_MB overrides the /proc/meminfo reading for tests.
MCP_MEM_MIN_MB="${MCP_MEM_MIN_MB:-1024}"
MCP_RELEASES_KEEP="${MCP_RELEASES_KEEP:-1}"           # retain the newest N mcp releases (the live one)
MCP_RELEASES_CAP="${MCP_RELEASES_CAP:-3}"             # hard cap on managed containers = the {8091,A,B} port pool
MCP_STOP_GRACE="${MCP_STOP_GRACE:-1260}"             # `docker stop --time`: >= _mcp_health grace (1200) + margin

# review-bot redeploys iff a matching path changed between deployed..target.
BOT_PATHS='src/rebar/ infra/compose/Dockerfile.reviewbot pyproject.toml infra/compose/docker-compose.yml infra/scripts/reviewbot-ensure-tickets.sh'
# secrets sources: the .env is SSM-sourced (fetch-secrets.sh) and rsync-EXCLUDED, so a
# new/rotated SSM-backed env key would never reach the box on deploy (f600). A new key
# requires editing fetch-secrets.sh (to emit the leaf) and/or ssm.tf (to declare the param)
# — NEITHER is in BOT_PATHS, so we trigger the review-bot redeploy (and a pre-`up`
# fetch-secrets refresh, below) on these paths too. (A pure SSM VALUE rotation with no git
# change does not advance main, so autodeploy no-ops on it — that path is operator-driven.)
SECRETS_PATHS='infra/scripts/fetch-secrets.sh infra/terraform/ssm.tf'
# mcp redeploys (blue-green) iff a matching path changed. esok's canonical `mcp:` compose-comment
# set. `src/rebar` and `infra/compose/docker-compose.yml` are SHARED with BOT_PATHS on purpose: a
# shared change triggers BOTH the review-bot AND the mcp target, in INDEPENDENT `if changed`
# blocks (neither touches the gerrit container or gerrit review flow).
MCP_PATHS='src/rebar infra/compose/Dockerfile.mcp infra/compose/docker-compose.yml uv.lock pyproject.toml'
# config paths are DETECT-ONLY in v1 (signalled, never auto-applied).
# infra/compose/gerrit.config is in this list, NOT in a re-materializing trigger, on
# purpose: compose-up.sh DOES re-seed it into the site etc dir, but only when compose-up
# runs, and this loop deliberately never touches the Gerrit container (BOT_SERVICE is
# "NEVER 'gerrit'"). Gerrit also reads gerrit.config once at injector-creation time, so
# applying it means RESTARTING Gerrit — an operator judgement call on a live review gate,
# not something the unattended loop may do. Before it was listed here a gerrit.config
# change reached /opt/rebar and then silently did nothing, with no signal at all
# (bug 1630-0279-85ba-4e15); detect-only at least makes that visible.
CONFIG_PATHS='infra/gerrit/replication.config infra/gerrit/project.config infra/gerrit/gerrit_to_platform.ini.template infra/gerrit/materialize-g2p-config.sh infra/compose/gerrit.config'
# host observability probe: re-materialized (idempotent installer) on a source change.
# Its installed copy at /usr/local/bin lives OUTSIDE the compose build context, so a probe
# change reaches no trigger above and would otherwise never be refreshed on the box.
OBS_PATHS='infra/scripts/observability.sh infra/scripts/install-observability.sh'
# host certbot renew timer: same drift class as the probe above. The installed units
# /etc/systemd/system/certbot-renew.{service,timer} live OUTSIDE the compose build
# context; only install-certbot-timer.sh writes them, and infra/scripts/ is in no
# trigger above, so a certbot-source change would otherwise never refresh the host.
CERTBOT_PATHS='infra/scripts/install-certbot-timer.sh'
# autodeploy's OWN installer/self-update (install-autodeploy.sh + the
# rebar-autodeploy.{service,timer} unit files it writes) is DELIBERATELY EXCLUDED from
# in-run re-materialization — there is intentionally NO AUTODEPLOY_PATHS block mirroring
# OBS_PATHS/CERTBOT_PATHS above. WHY (do not "fix" this by adding one):
#   1. Re-exec / self-modification race. This script runs AS rebar-autodeploy.service
#      (ExecStart=$DEPLOY_REPO/infra/scripts/autodeploy.sh). install-autodeploy.sh rewrites
#      /etc/systemd/system/rebar-autodeploy.{service,timer} + `systemctl daemon-reload` —
#      i.e. re-materializing it in-run means the running unit rewrites and reloads its OWN
#      service/timer definition mid-execution. The unattended path guarding a LIVE,
#      FAIL-CLOSED submission gate must not mutate the mechanism that is currently running it.
#   2. Staged-rollout / operator gate. install-autodeploy.sh deliberately installs the timer
#      DISABLED; an operator dry-runs (`systemctl start rebar-autodeploy.service`) and only
#      then `enable --now`s it (ADR 0026 "Back-out"; installer header differences 1 & 2). An
#      in-run auto-re-materialize would silently re-assert those units behind that human gate,
#      defeating the deliberate staged rollout.
# Autodeploy's lifecycle (its units + installer) is therefore owned by the PROVISIONING/
# operator layer (the one-time install-autodeploy.sh run + operator enable), not by this
# unattended in-run re-materializer — the same v1 boundary as CONFIG_PATHS being DETECT-ONLY.
# (The script BODY, autodeploy.sh, still updates in place via the BOT_PATHS rsync like any
# other source; this exclusion is specifically about the installer/unit-file self-update.)
# rsync excludes: protect the SSM secrets .env, the deploy marker, and dev/state dirs.
# The materialized mcp-static-tokens.json is SSM-sourced + rsync-EXCLUDED like .env (gotcha
# f600): a value-only re-materialize must not be clobbered by `rsync --delete`.
RSYNC_EXCLUDES=(--exclude '/.git' --exclude 'infra/compose/.env' \
  --exclude 'infra/compose/mcp-static-tokens.json' --exclude 'infra/compose/mcp-static-tokens.json.*' --exclude '/.deployed_ref' \
  --exclude '/.venv' --exclude '/.terraform' --exclude '/.serena' --exclude '/.claude' --exclude '/.tickets-tracker')

mkdir -p "$STATE_DIR"
now() { date +%s; }
log() { printf '{"event":"autodeploy","ts":%s,"msg":%s}\n' "$(now)" "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$*")"; }
# Emit a COUNTABLE journal marker: "<TOKEN> {json}" on stderr -> journald. observability.sh
# greps this unit's journal for each token and publishes the per-interval delta as a CloudWatch
# metric, so a token is an alarm contract — never reuse one for a different meaning, and never
# let captured text carry one (see capture_bot_logs' redaction).
marker() { printf '%s %s\n' "$1" "$(python3 -c 'import json,sys;print(json.dumps({"ts":int(sys.argv[1]),"reason":sys.argv[2],"detail":sys.argv[3]}))' "$(now)" "$2" "${3:-}")" >&2; }
err() { marker AUTODEPLOY_ERROR "$1" "${2:-}"; }

# How many reviews the review-bot is running RIGHT NOW, from its /health `in_flight` field.
# Echoes -1 for "unknown" (bot unreachable / field missing / unparseable), which the drain gate
# below treats as fail-OPEN — see there for why that direction is the safe one.
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

# Copy a BOUNDED tail of the failing review-bot container's own output into the deploy journal.
# The rollback below replaces that container immediately, so without this its stderr is
# recoverable only by host access to a container that no longer exists — which is why the
# 2026-07-31 journal could not tell "the image is broken" from "the image is fine, just slow",
# two failures with opposite remediations. Bounded three ways (lines, bytes, wall clock) and
# strictly best-effort: a capture failure must never alter the rollback that follows.
# The tail goes through log(), which JSON-encodes it onto ONE stdout line, and the literal
# AUTODEPLOY_ERROR token is redacted first: observability.sh counts that token in THIS unit's
# journal to drive the deploy_errors CloudWatch alarm, so echoing captured text unredacted
# could inflate the alarm.
capture_bot_logs() {
  local out
  out="$( cd "$COMPOSE_DIR" && timeout 15 docker compose logs --no-color \
            --tail "$HEALTH_FAIL_LOG_LINES" "$BOT_SERVICE" 2>&1 \
          | tail -c "$HEALTH_FAIL_LOG_BYTES" )" \
    || out="(could not read $BOT_SERVICE logs)"
  [ -n "$out" ] || out="(no output from $BOT_SERVICE)"
  log "bot-unhealthy diagnostics — last $HEALTH_FAIL_LOG_LINES lines of $BOT_SERVICE: ${out//AUTODEPLOY_ERROR/AUTODEPLOY_ERR<redacted>}"
}

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

# Echo the integer root-disk used percent (same computation as observability.sh's root-disk
# pressure probe, so the two never drift). Echoes 0 if `df` yields nothing parseable.
root_disk_pct() {
  local pct
  pct="$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')"
  echo "${pct:-0}"
}

# Disk-pressure gate for the no-op path (story 28f9): a quiescent `main` never reaches the
# deploy/backoff paths above where prune_docker_caches already runs, so this is the only
# reclaim opportunity between deploys. Throttled via PRESSURE_PRUNE_TS_FILE so a busy stretch
# of no-op ticks can't hammer the docker daemon; a triggered prune emits exactly one countable
# AUTODEPLOY_DISK_PRESSURE marker so observability.sh can publish it as a metric.
reclaim_under_pressure() {
  local pct last
  pct="$(root_disk_pct)"
  if [ "$pct" -lt "$DISK_PRESSURE_PCT" ]; then
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
  log "disk pressure reclaim complete"
}

# ── mcp blue-green helpers (foxterrier) ────────────────────────────────────────
# All of these are read-mostly discovery + idempotent teardown, so they are safe to call on the
# no-op tick (mcp_retire_sweep runs there too, reaping containers as they finish draining).

# Names of autodeploy-managed mcp containers (the boot compose backend + every blue-green run).
# $1 = "-a" to include stopped/exited containers (default: running only).
mcp_managed() {
  docker ps ${1:-} --format '{{.Names}}' 2>/dev/null \
    | grep -E "^(${MCP_CONTAINER_PREFIX}|${MCP_COMPOSE_CONTAINER})" || true
}
# The HOST port a managed container publishes container-port 8091 on (echoes nothing if unknown).
mcp_port_of() { docker port "$1" 8091/tcp 2>/dev/null | sed -E 's/.*:([0-9]+)$/\1/' | head -1; }
# The port the /mcp/ upstream include currently points at (the LIVE backend).
mcp_live_port() { sed -nE 's/.*server[[:space:]]+127\.0\.0\.1:([0-9]+);.*/\1/p' "$MCP_UPSTREAM_FILE" 2>/dev/null | head -1; }

# MemAvailable in MB (MCP_MEM_AVAILABLE_MB overrides for tests). Echoes -1 for "unreadable",
# which the caller treats as fail-OPEN.
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

# Whichever of the blue/green ports is NOT currently bound by a managed container (8091 is
# reserved for the compose-original). Echoes nothing when BOTH are occupied — the caller then
# emits AUTODEPLOY_MCP_RETIRE_CAP and backs off rather than colliding with a live port.
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

# Start a NEW mcp container reproducing the compose `mcp:` service EXACTLY. `docker compose up
# mcp` pins host 8091 and would collide, and `docker run` does NOT interpolate compose's
# `${VAR:-default}`, so every env value is spelled out here with its compose default. The
# compose-parity test (test_autodeploy_mcp_bluegreen.py) pins this set to docker-compose.yml so
# it cannot silently drift — /health is auth-independent and would not catch a wrong
# REBAR_MCP_AUTH_*/ALLOWED_HOSTS. $1 = container name, $2 = host port.
mcp_run_new() {
  docker run -d --name "$1" \
    --restart always \
    --stop-timeout "$MCP_STOP_GRACE" \
    --env-file "$COMPOSE_DIR/.env" \
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
    -p "127.0.0.1:${2}:8091" \
    -v "$COMPOSE_DIR/mcp-static-tokens.json:/run/secrets/mcp-static-tokens.json:ro" \
    -v "$COMPOSE_DIR/opcert-ed25519-key:/run/secrets/opcert-ed25519-key:ro" \
    "$MCP_IMAGE:$TARGET" >/dev/null 2>&1
}

# Atomically re-point the /mcp/ upstream include at $1 (a host port) and reload nginx. The temp
# is a DOTFILE (not matched by the `mcp-upstream*.conf` glob nginx includes) so a half-written
# file is never picked up; the `mv` is an atomic rename. On a validate/reload failure the
# previous include is restored byte-identical and the function fails (the caller then removes the
# NEW container and backs off — the OLD backend stays live).
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

# Retire a single OLD container GRACEFULLY: `docker stop --time` sends SIGTERM, which triggers the
# container's OWN bounded self-drain (stop intake, wait in_flight->0 up to the app grace, exit).
# Issued in the BACKGROUND so the tick NEVER waits on the drain (the deploy is already DONE at the
# flip). NEVER `docker rm -f` — that SIGKILLs an in-flight certified op (review-bot bug 7b4a).
mcp_retire_graceful() {
  log "mcp retire: 'docker stop --time ${MCP_STOP_GRACE}' $1 in background (graceful SIGTERM self-drain; never rm -f a serving container)"
  # Close the deploy flock FD (9) in the subshell: a backgrounded child that inherits it would
  # hold the lock for up to MCP_STOP_GRACE after the tick exits, so every subsequent autodeploy
  # tick (for ANY component) would skip with "another deploy holds the lock".
  ( exec 9>&-; docker stop --time "$MCP_STOP_GRACE" "$1" >/dev/null 2>&1 ) &
}

# Retire everything except the newest MCP_RELEASES_KEEP (the live backend): ask each still-running
# old container to drain (idempotent), and REAP any that have already finished draining (exited),
# freeing its blue/green port. Over MCP_RELEASES_CAP managed containers (too many draining /
# holding ports) emit AUTODEPLOY_MCP_RETIRE_CAP instead of forcing a kill. Safe on the no-op tick.
mcp_retire_sweep() {
  local live n p count
  live="$(mcp_live_port)"
  while read -r n; do
    [ -n "$n" ] || continue
    p="$(mcp_port_of "$n")"
    if [ "$(docker inspect -f '{{.State.Running}}' "$n" 2>/dev/null)" = "true" ]; then
      # Never STOP a running container we cannot prove is not the live backend. If the live
      # port is UNKNOWN (upstream include missing/unreadable) fail SAFE: leave every running
      # container alone rather than risk stopping the one still serving /mcp (bug 7b4a). An
      # already-exited container is safe to reap regardless (it serves nothing).
      [ -z "$live" ] && continue
      [ "$p" = "$live" ] && continue                    # keep the live backend (RELEASES_KEEP)
      mcp_retire_graceful "$n"
    else
      docker rm "$n" >/dev/null 2>&1 && log "mcp retire: reaped exited $n (port ${p:-?} freed)"
    fi
  done < <(mcp_managed -a)
  count="$(mcp_managed -a | grep -c . || true)"
  case "$count" in ''|*[!0-9]*) count=0 ;; esac
  if [ "$count" -gt "$MCP_RELEASES_CAP" ]; then
    marker AUTODEPLOY_MCP_RETIRE_CAP over-cap "managed mcp containers=$count > cap=$MCP_RELEASES_CAP; NOT forcing a kill (containers still draining/holding ports)"
  fi
}

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
# Up to date: no deploy is pending, so any deferral episode is over. Clearing it here (and at
# the success footer) is what keeps a STALE episode from making the NEXT episode's bound look
# already-exhausted and killing a review on the first busy tick.
if [ "$TARGET" = "$DEPLOYED" ]; then
  rm -f "$DEFER_FILE"
  reclaim_under_pressure
  mcp_retire_sweep          # reap mcp containers that finished draining since the last flip
  log "up to date ($TARGET); no-op"
  exit 0
fi

# backoff: same failed TARGET, not time yet -> skip. NEW target -> reset (fix-forward).
read -r bo_sha bo_cnt bo_next < <(cat "$BACKOFF_FILE" 2>/dev/null || echo "- 0 0")
if [ "$bo_sha" = "$TARGET" ] && [ "$(now)" -lt "${bo_next:-0}" ]; then
  log "backoff active for $TARGET (fail #$bo_cnt); next attempt at $bo_next"; exit 0
fi
[ "$bo_sha" != "$TARGET" ] && { bo_cnt=0; }

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
  err config_manual "infra config changed in $TARGET — replication/g2p/refs-meta/gerrit.config need a MANUAL operator apply (auto-apply is a v2 follow-up)"
  log "infra config change detected + signalled (not auto-applied in v1)"
fi

# ── review-bot: rebuild + restart ONLY on a source change ─────────────────────
if changed "$BOT_PATHS" || changed "$SECRETS_PATHS"; then
  # ── drain gate: never recreate the container mid-review (bug 34cd) ───────────
  # `docker compose up -d` STOPS the running container, and uvicorn's shutdown drains only
  # the webhook QUEUE (app.py waits on queue.join()) — the backfill reconciler's inline
  # review is cancelled outright, and that is the very path that retries a killed review.
  # A killed review is SILENT: nothing failed, so no VOTER_ERROR is emitted, `restarts`
  # stays 0, and the deploy still logs "redeployed + healthy". On 2026-08-03 seven
  # recreations in 90 minutes (gaps of 18/7/22/4/15/20 min) repeatedly killed a ~10-minute
  # review, and changes 1302/1303 sat `Verified +1` with `LLM-Review = 0` for 20-35 minutes
  # with all 11 CloudWatch alarms OK. So: ask the bot whether it is busy and DEFER if it is.
  #
  # DEFER (skip this tick, retry on the next ~2-minute timer fire) rather than DRAIN
  # (sleep here until idle): this unit is an idempotent oneshot timer, so "come back later"
  # is free, whereas sleeping would hold the deploy `flock` for the whole wait. Deferral also
  # COALESCES a burst for nothing extra — TARGET is recomputed from the mirror on every tick,
  # so a run of deferred ticks collapses into ONE deploy at the newest tip, which is what a
  # debounce would have bought, without delaying deploys when the bot is idle.
  inflight="$(bot_in_flight_reviews)"
  defer_since="$(cat "$DEFER_FILE" 2>/dev/null || echo 0)"
  case "$defer_since" in '' | *[!0-9]*) defer_since=0 ;; esac
  if [ "$inflight" -gt 0 ]; then
    # Bound the EPISODE (a continuous run of busy ticks), NOT the target SHA. Keying it to
    # TARGET the way BACKOFF_FILE does would defeat AC2's bound outright: during a landing
    # burst TARGET advances on almost every tick, so a SHA-keyed timer would reset before it
    # could ever expire and a permanently-busy bot would block deploys forever — exactly the
    # unbounded wait the bound exists to prevent. The episode is cleared whenever the bot is
    # found idle, a deploy proceeds, or the box is up to date, so it only accumulates while a
    # deploy is genuinely pending AND the bot is genuinely busy.
    [ "$defer_since" -eq 0 ] && { defer_since="$(now)"; echo "$defer_since" >"$DEFER_FILE"; }
    waited=$(( $(now) - defer_since ))
    if [ "$waited" -lt "$DEPLOY_DEFER_MAX" ]; then
      marker AUTODEPLOY_DEFERRED review-in-flight \
        "target=$TARGET in_flight=$inflight deferred_for=${waited}s bound=${DEPLOY_DEFER_MAX}s"
      log "review-bot busy ($inflight review(s) in flight); DEFERRING the deploy of $TARGET (${waited}s of the ${DEPLOY_DEFER_MAX}s bound used); deployed-sha unchanged; retrying on the next timer tick"
      exit 0
    fi
    # Bound exhausted: recreate anyway, but make the kill COUNTABLE. Without this marker the
    # interruption is unobservable — which is the whole reason a fully live-locked gate could
    # sit behind green health signals.
    marker AUTODEPLOY_REVIEW_INTERRUPT bound-exceeded \
      "target=$TARGET in_flight=$inflight deferred_for=${waited}s bound=${DEPLOY_DEFER_MAX}s; recreating anyway, so an in-flight review IS being killed"
    log "deferral bound ${DEPLOY_DEFER_MAX}s exhausted with $inflight review(s) still in flight; proceeding (a review is interrupted; the backfill reconciler retries it)"
  elif [ "$inflight" -lt 0 ]; then
    # Fail OPEN on an unreadable signal: a bot that cannot answer /health is likely broken or
    # down, and deploying is how a broken bot gets FIXED — blocking deploys on an unparseable
    # field would invent a NEW way to freeze the gate, strictly worse than the bug. But do not
    # let that be silent: a signal that quietly stops working (renamed field, wedged bot) puts
    # us back in the original blind state, so it emits the same countable interrupt marker.
    marker AUTODEPLOY_REVIEW_INTERRUPT signal-unavailable \
      "target=$TARGET; /health in_flight unreadable at $HEALTH_URL — deploying WITHOUT a drain check, so a review may be killed unobserved"
    log "in-flight review signal unavailable at $HEALTH_URL; proceeding without the drain check (fail-open: a broken bot is fixed BY deploying)"
  fi
  rm -f "$DEFER_FILE"
  log "review-bot sources or secrets changed; sync + refresh .env + rebuild + restart (blast radius = $BOT_SERVICE only)"
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

  # Refresh the SSM-sourced .env BEFORE `compose up` so new/rotated keys reach the container
  # without a manual boot (f600 AC2). .env is rsync-EXCLUDED + SSM-sourced, so it is otherwise
  # never regenerated on deploy. fetch-secrets is fail-fast: on ANY SSM error it exits non-zero
  # WITHOUT touching .env — so we abort the deploy here, BEFORE tagging :prev or building, which
  # keeps autodeploy's fail-safe/never-half-updated guarantee: the running bot stays on its
  # current image (:latest untouched → nothing to roll back). Runs only on an actual bot/secrets
  # redeploy (not every tick), so no SSM-quota regression. Uses the just-rsynced TARGET copy.
  if ! ENV_FILE="$DEPLOY_REPO/infra/compose/.env" bash "$DEPLOY_REPO/infra/scripts/fetch-secrets.sh" >/dev/null 2>&1; then
    err secrets-fetch-failed "fetch-secrets.sh failed (SSM unreachable / param missing); .env left intact; deploy aborted (bot stays on current image)"
    record_backoff_failure; exit 1
  fi
  # fetch-secrets rewrites .env as the deploy user (0600); re-assert the preserved owner.
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
    # Capture the evidence BEFORE the rollback replaces the failing container.
    capture_bot_logs
    err bot-unhealthy "review-bot failed health check within ${HEALTH_TIMEOUT}s after deploy; ROLLING BACK to :prev (container log tail captured above)"
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

# ── mcp: blue-green pointer-swap deploy ONLY on a source change ────────────────
# INDEPENDENT of the review-bot block above: a shared src/rebar / docker-compose.yml change
# triggers both, each on its own path. Unlike the review-bot's stop-and-drain, the mcp server is
# a never-idle shared endpoint, so this does an immutable-release + atomic-pointer-swap cutover
# (start new alongside old -> health -> flip nginx -> retire old when idle) and NEVER kills an
# in-flight certified op. Fatal-on-failure like the review-bot (record_backoff_failure + exit 1)
# so deployed-sha is not advanced and the deploy retries next tick. The gerrit container is never
# touched (blast-radius assert); gerrit is never involved.
if changed "$MCP_PATHS"; then
  log "mcp sources changed $DEPLOYED -> $TARGET; blue-green deploy (blast radius = mcp containers + /mcp nginx upstream only)"
  gerrit_before_mcp="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"

  # 1. sync + build the immutable release image, tagged by the target SHA.
  if ! git -C "$MIRROR_DIR" checkout -q "$TARGET" 2>/dev/null; then
    err mcp-checkout-failed "git checkout $TARGET in $MIRROR_DIR failed"; record_backoff_failure; exit 1
  fi
  if ! rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$MIRROR_DIR/" "$DEPLOY_REPO/" 2>/dev/null; then
    err mcp-rsync-failed "rsync $MIRROR_DIR -> $DEPLOY_REPO failed"; record_backoff_failure; exit 1
  fi
  # Refresh the SSM-sourced secrets BEFORE building/starting the new container. mcp consumes
  # TWO rsync-EXCLUDED, SSM-materialized artifacts -- .env (mcp_run_new --env-file) and
  # mcp-static-tokens.json (mcp_run_new -v) -- so, exactly as for the bot at the call above,
  # neither is regenerated by a deploy on its own. Without this an SSM PAT rotation never
  # reaches the container: the operator populates the slot, no git ref moves, the file on disk
  # stays as it was, and the next unrelated src/rebar change starts a container against the
  # STALE file. That is not hypothetical -- it is the 2026-08-25 crash-loop
  # (receptive-houndy-nilgai): three PATs landed in SSM at 16:42Z, the on-disk tokens file was
  # still {"tokens": []} at 16:44Z, and every blue-green container failed closed at
  # _mcp_auth.py:725 ("defines no tokens") until an operator ran fetch-secrets.sh by hand.
  # Fail-fast semantics mirror the bot path: on ANY SSM error, abort BEFORE building or
  # touching the live upstream, so the running container keeps serving.
  if ! ENV_FILE="$COMPOSE_DIR/.env" bash "$DEPLOY_REPO/infra/scripts/fetch-secrets.sh" >/dev/null 2>&1; then
    err mcp-secrets-fetch-failed "fetch-secrets.sh failed (SSM unreachable / param missing); .env left intact; mcp deploy aborted (old container stays live)"
    record_backoff_failure; exit 1
  fi
  if ! ( cd "$COMPOSE_DIR" && docker compose build mcp ); then
    err mcp-build-failed "docker compose build mcp failed"; record_backoff_failure; exit 1
  fi
  if ! docker tag "$MCP_IMAGE" "$MCP_IMAGE:$TARGET" >/dev/null 2>&1; then
    err mcp-tag-failed "docker tag $MCP_IMAGE -> $MCP_IMAGE:$TARGET failed"; record_backoff_failure; exit 1
  fi

  # 2. memory pre-check BEFORE the 2x overlap (8 GiB box). Below the floor: ABORT before ever
  #    starting the second container. Unreadable (-1) fails OPEN.
  mcp_mem="$(mcp_mem_available_mb)"
  if [ "$mcp_mem" -ge 0 ] && [ "$mcp_mem" -lt "$MCP_MEM_MIN_MB" ]; then
    marker AUTODEPLOY_MCP_MEM_ABORT low-memory "MemAvailable=${mcp_mem}MB < min ${MCP_MEM_MIN_MB}MB on the 8GiB box; refusing the blue-green 2x overlap"
    record_backoff_failure; exit 1
  fi
  [ "$mcp_mem" -lt 0 ] && log "mcp mem-check: MemAvailable UNREADABLE; failing OPEN (proceeding with the 2x overlap without a memory guarantee)"

  # 3. pick a FREE blue/green host port. Both busy -> emit the cap marker + back off, start
  #    NOTHING (never collide on a live port; managed set is capped at the {8091,A,B} pool).
  mcp_newport="$(mcp_free_port)"
  if [ -z "$mcp_newport" ]; then
    marker AUTODEPLOY_MCP_RETIRE_CAP port-exhausted "both $MCP_PORT_A and $MCP_PORT_B held by un-reaped mcp containers; not starting a colliding 3rd (cap=$MCP_RELEASES_CAP)"
    record_backoff_failure; exit 1
  fi
  mcp_newname="${MCP_CONTAINER_PREFIX}-${TARGET:0:12}-${mcp_newport}"

  # 4. start the NEW container ALONGSIDE the old.
  if ! mcp_run_new "$mcp_newname" "$mcp_newport"; then
    err mcp-run-failed "docker run $mcp_newname on 127.0.0.1:${mcp_newport} failed"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    record_backoff_failure; exit 1
  fi

  # 5. health-check the NEW container. On failure remove IT and leave the OLD upstream live +
  #    byte-identical (nothing was flipped) — a rollback, not a cutover.
  mcp_ok=0; mcp_deadline=$(( $(now) + MCP_HEALTH_TIMEOUT ))
  while [ "$(now)" -lt "$mcp_deadline" ]; do
    curl -fsS -m 3 "http://127.0.0.1:${mcp_newport}/health" >/dev/null 2>&1 && { mcp_ok=1; break; }
    sleep 2
  done
  if [ "$mcp_ok" != 1 ]; then
    err mcp-unhealthy "new mcp container $mcp_newname failed /health within ${MCP_HEALTH_TIMEOUT}s; removing it, leaving the OLD upstream live"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    record_backoff_failure; exit 1
  fi

  # 6. ATOMICALLY flip the /mcp/ upstream to the new container. The cutover is DONE at the reload;
  #    it never waits on the OLD backend's in-flight ops. On failure restore the previous include
  #    (byte-identical) + remove the new container + back off.
  if ! mcp_flip_upstream "$mcp_newport"; then
    err mcp-flip-failed "nginx flip to 127.0.0.1:${mcp_newport} failed; restored previous upstream + removing new container"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    record_backoff_failure; exit 1
  fi
  log "mcp cutover complete: /mcp upstream now 127.0.0.1:${mcp_newport} (deploy DONE; not waiting on in-flight drain)"

  # 7. retire the OLD backend off the critical path (graceful docker stop, reap when drained).
  mcp_retire_sweep

  # blast-radius assertion: the gerrit container must be UNTOUCHED.
  gerrit_after_mcp="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"
  if [ -n "$gerrit_before_mcp" ] && [ "$gerrit_before_mcp" != "$gerrit_after_mcp" ]; then
    err blast-radius "gerrit container id changed during an mcp deploy — investigate"
  fi
  prune_docker_caches
  log "mcp redeployed + healthy at 127.0.0.1:${mcp_newport}"
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

# ── host certbot renew timer: re-materialize on an installer-source change ─────
# The systemd timer runs `certbot renew` from unit files that ONLY
# install-certbot-timer.sh writes; nothing else refreshes them. infra/scripts/ is
# in no trigger above (not a BOT_PATH, so a certbot-only change syncs nothing at
# all), so the installed units would silently go stale — the same drift class the
# OBS block above fixed (sibling parity, commit ffcf2c662 / ticket 1d63).
# install-certbot-timer.sh is idempotent + runs as root (rewrites the unit files,
# daemon-reload; certbot no-ops when the cert is current), so re-running it from
# the TARGET source reconverges the host timer. DOMAIN/EMAIL flow through from the
# /etc/rebar/autodeploy.env sourced above, exactly as the installer's defaults expect.
# Non-fatal: a timer-refresh failure must not roll back the review-bot, but it emits
# an AUTODEPLOY err marker so the staleness is alarmed instead of silent.
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

# ── success: advance deployed-sha atomically, clear backoff ───────────────────
echo "$TARGET" > "$SHA_FILE.tmp" && mv "$SHA_FILE.tmp" "$SHA_FILE"
clear_backoff
rm -f "$DEFER_FILE"          # the deferral episode ended with a deploy; do not carry it forward
log "deploy complete: env now reflects $TARGET"
