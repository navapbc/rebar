#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# autodeploy.sh — continuous auto-deploy: make the running Gerrit box reflect `main`
# without manual deploy or restart (epic 88ab / story 8903).
#
# Run every ~2 min by rebar-autodeploy.timer -> rebar-autodeploy.service (oneshot).
# Polls the PUBLIC GitHub mirror read-only (no GitHub->AWS trust surface) and, when
# `main` advances, idempotently re-applies ONLY the components whose files changed:
#   - review-bot container  (src/rebar/**, Dockerfile.reviewbot, pyproject, compose)
#   - replication.config    (autoReload; no Gerrit restart)
#   - g2p config            (materialise)
# refs/meta/config (project.config) is DETECT-ONLY in v1 (needs an operator admin key).
#
# STABILITY (the box runs a FAIL-CLOSED gate — a bad deploy could freeze submissions):
#   - bounded blast radius: NEVER touches the `gerrit` container; only the review-bot
#     service is rebuilt/restarted; refs/meta/config is never auto-applied.
#   - self-heal: an end-to-end health check gates success; on failure the review-bot is
#     ROLLED BACK to its `:prev` image so the gate is restored; deployed-sha not advanced.
#   - capped exponential backoff (NOT hard-disable), keyed to the target SHA: a new `main`
#     tip RESETS the backoff (fix-forward deploys promptly); a known-bad SHA is retried no
#     faster than the cap. (Mature-OSS consensus: Flux retryInterval, Argo CD retry
#     backoff, k8s controller-runtime ItemExponentialFailureRateLimiter, systemd v254
#     RestartSteps/RestartMaxDelaySec.)
#   - flock: overlapping timer fires never overlap.
#   - config validated (config-check) before it touches the live Gerrit.
# ---------------------------------------------------------------------------
set -uo pipefail   # NOT -e: we handle failures explicitly (fail-safe, never half-updated)

# ── tunables (single source of truth; advisory T5e) ───────────────────────────
DEPLOY_REPO="${DEPLOY_REPO:-/opt/rebar/src}"           # the compose build context checkout
COMPOSE_DIR="${COMPOSE_DIR:-$DEPLOY_REPO/infra/compose}"
MIRROR_REMOTE="${MIRROR_REMOTE:-origin}"               # the PUBLIC GitHub mirror (read-only)
STATE_DIR="${STATE_DIR:-/var/lib/rebar}"
LOCK="$STATE_DIR/deploy.lock"
SHA_FILE="$STATE_DIR/deployed-sha"
BACKOFF_FILE="$STATE_DIR/deploy-backoff"               # "<target-sha> <fail-count> <next-epoch>"
BOT_SERVICE="${BOT_SERVICE:-review-bot}"               # compose service name (NEVER 'gerrit')
BOT_IMAGE="${BOT_IMAGE:-compose-review-bot}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"   # review-bot receiver (NOT Gerrit 8080)
FETCH_TIMEOUT="${FETCH_TIMEOUT:-60}"                   # advisory T5b: hung fetch must not hold the lock
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-30}"
BACKOFF_BASE="${BACKOFF_BASE:-60}"; BACKOFF_FACTOR="${BACKOFF_FACTOR:-2}"; BACKOFF_CAP="${BACKOFF_CAP:-900}"

# component -> path-set (change detection). A component redeploys iff a matching path changed.
BOT_PATHS='src/rebar/ infra/compose/Dockerfile.reviewbot pyproject.toml infra/compose/docker-compose.yml'
REPL_PATHS='infra/gerrit/replication.config'
G2P_PATHS='infra/gerrit/gerrit_to_platform.ini.template infra/gerrit/materialize-g2p-config.sh'
META_PATHS='infra/gerrit/project.config'              # detect-only

mkdir -p "$STATE_DIR"
now() { date +%s; }
log() { printf '{"event":"autodeploy","ts":%s,"msg":%s}\n' "$(now)" "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$*")"; }
err() { printf 'AUTODEPLOY_ERROR %s\n' "$(python3 -c 'import json,sys;print(json.dumps({"ts":int(sys.argv[1]),"reason":sys.argv[2],"detail":sys.argv[3]}))' "$(now)" "$1" "${2:-}")" >&2; }

# ── single-flight ─────────────────────────────────────────────────────────────
exec 9>"$LOCK"
flock -n 9 || { log "another deploy holds the lock; skipping"; exit 0; }

cd "$DEPLOY_REPO" 2>/dev/null || { err repo-missing "$DEPLOY_REPO absent"; exit 1; }

# ── backoff gate (SHA-keyed; reset on a new target) ───────────────────────────
# fetch the target tip first (bounded), so we can key backoff to it.
if ! timeout "$FETCH_TIMEOUT" git fetch -q "$MIRROR_REMOTE" main 2>/dev/null; then
  err fetch-failed "git fetch $MIRROR_REMOTE main timed out/failed"; exit 1
fi
TARGET="$(git rev-parse "$MIRROR_REMOTE/main")"
DEPLOYED="$(cat "$SHA_FILE" 2>/dev/null || true)"

# First run: adopt current state WITHOUT deploying (advisory T9 — no :prev exists yet).
if [ -z "$DEPLOYED" ]; then
  echo "$TARGET" > "$SHA_FILE.tmp" && mv "$SHA_FILE.tmp" "$SHA_FILE"
  log "first run: adopting current HEAD $TARGET as deployed-sha (no deploy)"; exit 0
fi
[ "$TARGET" = "$DEPLOYED" ] && { log "up to date ($TARGET); no-op"; exit 0; }

# backoff: if the same failed TARGET is backing off and it's not time yet, skip.
read -r bo_sha bo_cnt bo_next < <(cat "$BACKOFF_FILE" 2>/dev/null || echo "- 0 0")
if [ "$bo_sha" = "$TARGET" ] && [ "$(now)" -lt "${bo_next:-0}" ]; then
  log "backoff active for $TARGET (fail #$bo_cnt); next attempt at $bo_next"; exit 0
fi
[ "$bo_sha" != "$TARGET" ] && { bo_cnt=0; }   # NEW target -> reset backoff (fix-forward)

record_backoff_failure() {
  local n=$(( ${bo_cnt:-0} + 1 ))
  local wait=$(( BACKOFF_BASE * (BACKOFF_FACTOR ** (n-1)) )); [ "$wait" -gt "$BACKOFF_CAP" ] && wait=$BACKOFF_CAP
  echo "$TARGET $n $(( $(now) + wait ))" > "$BACKOFF_FILE"
  err deploy-failed "target=$TARGET fail#$n backoff=${wait}s"
  log "deploy failed; backoff ${wait}s (fail #$n); last-known-good stays live"
}
clear_backoff() { rm -f "$BACKOFF_FILE"; }

# ── what changed? ─────────────────────────────────────────────────────────────
changed() { git diff --name-only "$DEPLOYED" "$TARGET" -- $1 2>/dev/null | grep -q .; }
log "main advanced $DEPLOYED -> $TARGET; computing component deltas"

# checkout the target into the build context (source sync)
git checkout -q "$TARGET" 2>/dev/null || { record_backoff_failure; exit 1; }

# ── config validation BEFORE any live apply (defense-in-depth) ────────────────
if changed "$REPL_PATHS $G2P_PATHS $META_PATHS"; then
  if ! bash infra/scripts/config-check.sh >/dev/null 2>&1; then
    err config-invalid "config-check failed on $TARGET; NOT applying"; record_backoff_failure; git checkout -q "$DEPLOYED"; exit 1
  fi
fi

# ── project.config (refs/meta/config): DETECT-ONLY (v1 boundary) ──────────────
if changed "$META_PATHS"; then
  err meta-config-manual "project.config changed in $TARGET — refs/meta/config needs a MANUAL operator apply (out of auto-deploy v1)"
  log "project.config change detected + signalled (not auto-applied)"
fi

# ── replication.config + g2p (autoReload / materialise; no restart) ───────────
if changed "$REPL_PATHS $G2P_PATHS"; then
  if bash infra/gerrit/materialize-g2p-config.sh >/dev/null 2>&1; then
    log "replication/g2p config re-materialised (autoReload; no restart)"
  else
    err materialise-failed "materialize-g2p-config.sh failed"; record_backoff_failure; exit 1
  fi
fi

# ── review-bot container: rebuild + restart ONLY on a source change ───────────
if changed "$BOT_PATHS"; then
  log "review-bot sources changed; rebuild + restart (blast radius = $BOT_SERVICE only)"
  gerrit_before="$(docker inspect -f '{{.Id}}' compose-gerrit-1 2>/dev/null || true)"
  # preserve the current image as :prev for rollback (only if one exists)
  if docker image inspect "$BOT_IMAGE:latest" >/dev/null 2>&1; then docker tag "$BOT_IMAGE:latest" "$BOT_IMAGE:prev"; have_prev=1; else have_prev=0; fi
  if ! ( cd "$COMPOSE_DIR" && docker compose build "$BOT_SERVICE" && docker compose up -d "$BOT_SERVICE" ); then
    err bot-build-failed "compose build/up $BOT_SERVICE failed"
    [ "$have_prev" = 1 ] && ( cd "$COMPOSE_DIR" && docker compose up -d "$BOT_SERVICE" )   # keep prior container up
    record_backoff_failure; git checkout -q "$DEPLOYED"; exit 1
  fi
  # END-TO-END health check (advisory: not just process-up).
  ok=0; deadline=$(( $(now) + HEALTH_TIMEOUT ))
  while [ "$(now)" -lt "$deadline" ]; do curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1 && { ok=1; break; }; sleep 2; done
  if [ "$ok" != 1 ]; then
    err bot-unhealthy "review-bot failed health check after deploy; ROLLING BACK to :prev"
    if [ "$have_prev" = 1 ]; then docker tag "$BOT_IMAGE:prev" "$BOT_IMAGE:latest"; ( cd "$COMPOSE_DIR" && docker compose up -d "$BOT_SERVICE" ); fi
    record_backoff_failure; git checkout -q "$DEPLOYED"; exit 1
  fi
  # blast-radius assertion: the gerrit container must be UNTOUCHED.
  gerrit_after="$(docker inspect -f '{{.Id}}' compose-gerrit-1 2>/dev/null || true)"
  if [ -n "$gerrit_before" ] && [ "$gerrit_before" != "$gerrit_after" ]; then
    err blast-radius "gerrit container id changed during a review-bot deploy — investigate"; # do not fail the deploy for this, but alarm loudly
  fi
  docker image prune -f >/dev/null 2>&1 || true   # advisory T9: reap dangling layers
  log "review-bot redeployed + healthy"
fi

# ── success: advance deployed-sha atomically, clear backoff ───────────────────
echo "$TARGET" > "$SHA_FILE.tmp" && mv "$SHA_FILE.tmp" "$SHA_FILE"
clear_backoff
log "deploy complete: env now reflects $TARGET"
