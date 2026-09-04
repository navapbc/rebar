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
# Per-component completion marker for the mcp blue-green path. autodeploy deploys TWO
# INDEPENDENT components per tick and one global deployed-sha cannot represent both: on a tick
# where the review-bot DEFERS, the mcp block still deploys and cuts over, but the tick exits at
# the bot_deferred guard WITHOUT advancing deployed-sha (correct — the BOT has not deployed).
# Without a component marker the next tick recomputes the SAME mcp delta and rebuilds + swaps
# the container again — with a chronically busy bot, every ~2 minutes, indefinitely. Absent
# (every box before this change), it READS AS $DEPLOYED, so the first tick after an upgrade
# behaves exactly as it does today: no spurious redeploy, no skipped deploy.
MCP_SHA_FILE="$STATE_DIR/mcp-deployed-sha"
# The sha that was live IMMEDIATELY BEFORE the current mcp release — the deterministic
# blue-green ROLLBACK target, recorded at cutover from the OUTGOING $mcp_deployed. Orphan
# reconciliation (mcp_reconcile_orphans) preserves exactly {live, this} and retires the rest.
# Recording deploy ORDER — not a build timestamp — is what makes "immediately-previous"
# unambiguous: two releases can share a whole-second CreatedAt, but only one was live before
# the current one. Until an mcp deploy records it (e.g. the first tick after this change lands),
# reconciliation DEFERS rather than guess, so a rollback image is never removed by heuristic.
MCP_PREV_SHA_FILE="$STATE_DIR/mcp-previous-sha"
# The review-bot's OWN last-deployed sha. One footer-written marker cannot represent TWO
# independently-deploying components: an mcp failure exits before the footer, so a bot that
# deployed successfully seconds earlier was never recorded, and the next tick redeployed it —
# stop-and-draining the container and KILLING an in-flight review on every tick until mcp
# recovered. Each component records its own completion, as soon as it completes.
BOT_SHA_FILE="$STATE_DIR/bot-deployed-sha"
BACKOFF_FILE="$STATE_DIR/deploy-backoff"              # "<target-sha> <fail-count> <next-epoch>"
# The mcp component's OWN backoff, same format. It is SEPARATE from $BACKOFF_FILE on purpose:
# $BACKOFF_FILE gates the whole script at the top, so writing it from the mcp path would let an
# mcp failure suppress the REVIEW-BOT deploy — a component that never even ran on that tick.
# The two paths deploy independently and track completion independently ($MCP_SHA_FILE); their
# failure state is independent too.
MCP_BACKOFF_FILE="$STATE_DIR/mcp-deploy-backoff"
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
# ── the Docker storage budget (ADR 0112 decision 1, story 9183-aaae-667d-45e6) ──
# infra/scripts/docker-storage-cap.sh is the SINGLE source of truth for the /var/lib/docker
# budget and its internal split, and it also renders the daemon's OWN builder.gc policy. Both
# caps therefore come from one place: the on-demand `docker builder prune --keep-storage`
# below and the GC the daemon runs on its own schedule cannot disagree about the BuildKit
# share, because neither number is spelled here.
DOCKER_CAP_SH="${DOCKER_CAP_SH:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)/docker-storage-cap.sh}"
eval "$(bash "$DOCKER_CAP_SH" --print-env 2>/dev/null)" || true
# buildkit cache hard cap (docker builder prune --keep-storage). EMPTY means the budget could
# not be read — a broken install, not a policy. prune_docker_caches then SKIPS the capped
# builder prune rather than re-inventing the number here: losing a warm cache is cheap, and a
# second copy of the ceiling is exactly the drift this indirection removes. The uncapped state
# is not a blind spot either — rebar-docker-buildkit-cache-high is what surfaces it.
BUILD_CACHE_KEEP="${BUILD_CACHE_KEEP:-${DOCKER_BUILDKIT_CACHE_BYTES:-}}"
# Disk-pressure reclaim on the no-op path (incident 2731 follow-up, story 28f9): a quiescent
# `main` never hits the deploy/backoff paths where prune_docker_caches already runs, so a
# build burst could fill the root disk and stay pinned until the NEXT real deploy (~39h
# observed). DISK_PRESSURE_PCT is the root-disk used-% threshold (matches observability.sh's
# root-disk pressure alarm framing); PRESSURE_PRUNE_MIN_INTERVAL throttles how often the no-op
# path may reclaim; PRESSURE_PRUNE_TS_FILE persists the last-reclaim timestamp across ticks.
DISK_PRESSURE_PCT="${DISK_PRESSURE_PCT:-80}"
DISK_PRESSURE_HARD_PCT="${DISK_PRESSURE_HARD_PCT:-90}"
PRESSURE_PRUNE_MIN_INTERVAL="${PRESSURE_PRUNE_MIN_INTERVAL:-600}"
PRESSURE_PRUNE_TS_FILE="${PRESSURE_PRUNE_TS_FILE:-$STATE_DIR/pressure-prune-ts}"
# Persistent-pressure streak (bug 9bc0-1200-1451-44bb). The 2026-08-28 outage ran the reclaim
# every ~10 min for ~11h while it freed NOTHING, and no signal expressed that: the threshold
# alarm flaps with the disk, and AUTODEPLOY_DISK_PRESSURE counts INVOCATIONS, so "the gate never
# ran" and "it ran and reclaimed nothing" are indistinguishable. This counter is the missing
# discriminator — it counts CONSECUTIVE INEFFECTIVE RECLAIM CYCLES (a completed reclaim after
# which the disk is STILL pressured), so it rises only on PERSISTENCE. The counter must survive
# between ticks (each tick is a separate process), so it is persisted exactly like
# PRESSURE_PRUNE_TS_FILE above. PRESSURE_STREAK_ALARM is the named threshold — deliberately a
# tunable and not a literal, matching DISK_PRESSURE_PCT / PRESSURE_PRUNE_MIN_INTERVAL.
PRESSURE_STREAK_FILE="${PRESSURE_STREAK_FILE:-$STATE_DIR/pressure-prune-streak}"
PRESSURE_STREAK_ALARM="${PRESSURE_STREAK_ALARM:-3}"

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
# infra/scripts/mcp-entrypoint.sh is the container ENTRYPOINT baked into the image by
# Dockerfile.mcp (`install -m 0755 … mcp-entrypoint.sh /usr/local/bin/mcp-entrypoint.sh` +
# `ENTRYPOINT`), so an entrypoint-source change needs the SAME image rebuild as a Dockerfile.mcp
# change — apply-parity inside the mcp target's v1 auto-apply scope (ADR 0079). Before it was
# listed here an entrypoint-only change reached /opt/rebar and the running container kept the
# stale entrypoint with no rebuild (bug 5d4c-a25b-b612-4aca).
MCP_PATHS='src/rebar infra/compose/Dockerfile.mcp infra/scripts/mcp-entrypoint.sh infra/compose/docker-compose.yml uv.lock pyproject.toml'
# NOTE: the mcp delta gate ORs SECRETS_PATHS in on top of MCP_PATHS (see mcp_delta() below),
# exactly as the review-bot gate does — mcp is the other consumer of fetch-secrets output, so a
# secrets-only change must redeploy it too (bug f910). SECRETS_PATHS is intentionally kept a
# separate variable rather than folded into MCP_PATHS so the two gates share one secrets list.
# config paths are DETECT-ONLY in v1 (signalled, never auto-applied).
# infra/compose/gerrit.config is in this list, NOT in a re-materializing trigger, on
# purpose: compose-up.sh DOES re-seed it into the site etc dir, but only when compose-up
# runs, and this loop deliberately never touches the Gerrit container (BOT_SERVICE is
# "NEVER 'gerrit'"). Gerrit also reads gerrit.config once at injector-creation time, so
# applying it means RESTARTING Gerrit — an operator judgement call on a live review gate,
# not something the unattended loop may do. Before it was listed here a gerrit.config
# change reached /opt/rebar and then silently did nothing, with no signal at all
# (bug 1630-0279-85ba-4e15); detect-only at least makes that visible.
# infra/gerrit/materialize-deploy-key.sh is the SSM→file deploy-key materializer, a direct
# sibling of materialize-g2p-config.sh already in this list; applying it also touches gerrit
# (it writes into the gerrit user's dir), so it is the SAME detect-only boundary. Before it was
# listed here a deploy-key-materializer change reached /opt/rebar and silently did nothing, with
# no signal at all (bug 408c-9c78-c523-4d1c); detect-only at least makes that visible.
CONFIG_PATHS='infra/gerrit/replication.config infra/gerrit/project.config infra/gerrit/gerrit_to_platform.ini.template infra/gerrit/materialize-g2p-config.sh infra/gerrit/materialize-deploy-key.sh infra/compose/gerrit.config'
# nginx edge source of truth: rebar.conf.template is rendered into
# /etc/nginx/conf.d/rebar.conf. Its rendered copy lives OUTSIDE the compose build context,
# so an edge change reaches no other trigger above and would otherwise never be detected.
# It is DETECT-ONLY in v1 (the same boundary as CONFIG_PATHS): applying the edge means a
# host `nginx -t` + reload, deferred to the v2 auto-apply epic (sprucing-wise-dikkops /
# 6d60-2d0c-6ff7-444b). Before this, a merged edge change reached /opt/rebar and then
# silently did nothing, with NO signal at all (bug 1d1b-a719-b675-4a1f); detect-only at
# least makes it visible.
EDGE_PATHS='infra/nginx/rebar.conf.template'
# host-nginx include materializers + seed: compose-up.sh runs materialize-opcert-guard.sh
# (writes /etc/nginx/opcert-guard.map.conf) and materialize-mcp-upstream.sh (installs the
# infra/nginx/mcp-upstream.conf seed into /etc/nginx/mcp-upstream.conf), both reloading host
# nginx. Their installed copies live OUTSIDE the compose build context and this loop never
# invokes compose-up.sh, so a change to any of these SOURCES reaches no trigger above and
# would otherwise never re-materialize on the box. DETECT-ONLY in v1 (the same boundary as
# CONFIG_PATHS/EDGE_PATHS): applying means re-running an infra/ script that calls
# `nginx -s reload`, deferred to the v2 auto-apply epic (sprucing-wise-dikkops /
# 6d60-2d0c-6ff7-444b). compose-up.sh is enumerated for signal parity only — the loop must
# NEVER re-run it (it brings up the Gerrit container; BOT_SERVICE is "NEVER 'gerrit'"), so
# like CONFIG_PATHS a change is signalled for a MANUAL operator apply, never auto-applied.
# Before this, a merged materializer-source change reached /opt/rebar and then silently did
# nothing, with NO signal at all (bug 5524-e353-2e2d-4dbe); detect-only at least makes it
# visible.
MATERIALIZER_PATHS='infra/scripts/compose-up.sh infra/scripts/materialize-opcert-guard.sh infra/scripts/materialize-mcp-upstream.sh infra/nginx/mcp-upstream.conf'
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

# bot_unreachable_disposition (bug 2f46): when bot_in_flight_reviews() returns -1 (the /health
# in_flight field could not be read), tell a bot that is merely MID-REDEPLOY — being recreated
# or still starting, so unreachable only TRANSIENTLY — from one that is genuinely WEDGED (its
# container up but not answering, reporting unhealthy, or absent). Echoes `redeploying` or
# `wedged`. BEST-EFFORT and FAIL-SAFE: any docker error, or a state we do not recognise, echoes
# `wedged` — which is exactly the pre-2f46 fail-open behaviour — so a broken observability path
# can never turn into a deferral live-lock. The caller only DEFERS on `redeploying`, and that
# defer is bounded by DEPLOY_DEFER_MAX just like the busy path.
bot_unreachable_disposition() {
  local cid status health
  cid="$( cd "$COMPOSE_DIR" && docker compose ps -q "$BOT_SERVICE" 2>/dev/null )" || { echo wedged; return 0; }
  [ -n "$cid" ] || { echo wedged; return 0; }   # no container at all -> deploy to (re)create it
  status="$( docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null )" || { echo wedged; return 0; }
  case "$status" in
    restarting | created | removing | paused) echo redeploying; return 0 ;;
  esac
  # `running` but /health unreadable is ambiguous; the docker HEALTHcheck disambiguates it —
  # `starting` means the container is still inside its start-period (mid-(re)start), whereas
  # `unhealthy`/`healthy`/none all mean it is up and simply not answering us == wedged.
  health="$( docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null )" || { echo wedged; return 0; }
  case "$health" in
    starting) echo redeploying; return 0 ;;
  esac
  echo wedged
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
# here (:prev is the review-bot's rollback lifeline, and the mcp per-release tags
# are reclaimed at RETIRE instead — mcp_retire_image, so a serving release can
# never be removed by a blanket prune). Each prune is time-bounded (a wedged
# daemon under disk pressure must not hold the deploy lock) and can NEVER alter
# control flow or mask the caller's failure exit code — a prune failure only logs.
#
# It also MEASURES itself (bug 9bc0): root-disk free space is read before and
# after and the delta logged, because "disk pressure reclaim complete" was emitted
# for ~11h on 2026-08-28 while freeing nothing. Effect is now observed, not inferred.
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

# Echo the integer root-disk AVAILABLE kilobytes (the `df --output=avail` sibling of
# root_disk_pct below). Echoes 0 if `df` yields nothing parseable, so an unreadable probe
# degrades to a visible "freed=0kB" rather than breaking the arithmetic in the caller.
root_disk_free_kb() {
  local kb
  kb="$(df --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')"
  echo "${kb:-0}"
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
  local pct last after streak
  pct="$(root_disk_pct)"
  if [ "$pct" -lt "$DISK_PRESSURE_PCT" ]; then
    # RESET LIVES HERE, not next to the increment below. A recovered tick returns from this
    # branch and never reaches the post-reclaim code, so a reset placed there would be
    # unreachable and the streak would never clear once it had risen.
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
  # A reclaim CYCLE just completed, so the streak is decided on its OUTCOME: re-read the disk
  # and compare against the same threshold. The throttled branch above returns before this and
  # therefore leaves the streak UNCHANGED — it counts reclaim cycles, not ticks, so a pressured
  # tick that ran no reclaim is evidence neither for nor against effectiveness (and counting
  # ticks would reach the alarm inside a single 600s throttle window on the ~2-min timer).
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

# The persistent-pressure streak, persisted across ticks (each tick is a separate process).
# Reads FAIL SAFE TOWARD SILENCE: absent, unreadable, or non-numeric is 0, so a lost or corrupt
# counter DELAYS the marker rather than emitting a false one. Writes are tmp+rename, so a torn
# write is never observed (same shape as the PRESSURE_PRUNE_TS_FILE write above).
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
# The image reference of the container publishing $1 (the live host port). Echoes nothing when
# the port is unknown or no managed container publishes it — the caller then retains nothing on
# this basis and falls back to its other guards (never the reverse).
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

# Retire the per-release image $1 that a JUST-REAPED container was running; $2 is the LIVE
# backend's image reference (may be empty = unknown). Called ONLY after `docker rm` succeeded
# for that container, so nothing it served is still running (bug 9bc0).
#
# Blue-green tags every build `$MCP_IMAGE:$TARGET` (compose-mcp:<sha>) and `docker image prune`
# without `-a` only drops DANGLING images, so these tagged releases accumulated unbounded —
# 44 images / 11.09GB on 2026-08-28, 10.08GB of it reclaimable, which is what filled the root
# disk and stalled nginx. Nothing was permitted to remove them; this is that permission,
# narrowed to exactly the images whose container has been reaped.
#
# THREE independent guards keep a serving release safe:
#   1. the bare $MCP_IMAGE build tag (the `docker compose build mcp` output that the deploy
#      re-tags each release) is never removed — removing it would break the next build's tag;
#   2. the LIVE backend's image is never removed;
#   3. removal is `docker image rm` WITHOUT -f, so the daemon itself refuses while ANY
#      container still references the image. Guard 3 holds even if 1 and 2 were both wrong.
# `:prev` is listed defensively: today it is a $BOT_IMAGE-only scheme and no compose-mcp:prev
# is ever created, so that arm is inert unless an mcp image-level rollback tag is introduced.
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

# Every per-release image ref under the mcp repo, one `<repo>:<tag>` per line. No ordering is
# implied or needed: the immediately-previous release is identified by RECORDED DEPLOY ORDER
# ($MCP_PREV_SHA_FILE), not by a build timestamp, so this is a pure membership enumeration.
mcp_image_tags() {
  docker images "$MCP_IMAGE" --format '{{.Repository}}:{{.Tag}}' 2>/dev/null
}

# Reconcile ORPHANED per-release image tags. mcp_retire_image only fires at the REAP of a
# live-tracked container, so a `compose-mcp:<sha>` tag whose container was already reaped on an
# earlier tick (or never had one on this box) leaks forever — `docker image prune` without `-a`
# never touches a tagged image. Unbounded, these re-fill the 30GiB root once a SECOND blue-green
# image family (the mcp target, ticket fd4a) shares it, re-tripping rebar-root-disk-pressure and
# fail-closing LLM-Review (bug e4f3, a regression of 3a52). This sweeps them every tick.
#
# It PRESERVES blue-green rollback — the LIVE backend's image AND the immediately-previous
# release are ALWAYS kept. Guards, any ONE sufficient:
#   1. only true per-release `<40-hex-sha>` tags are ever candidates — this structurally
#      excludes the bare `$MCP_IMAGE` build tag, `:latest` / `:prev`, and any `<none>` row
#      `docker images` emits for a dangling image of the repo;
#   2. never the LIVE backend's image ref;
#   3. never the immediately-previous release ($MCP_PREV_SHA_FILE, the rollback image);
#   4. `docker image rm` WITHOUT -f, so the daemon refuses while ANY container references it.
# The previous release is identified by RECORDED DEPLOY ORDER, not by a build timestamp: docker's
# CreatedAt has whole-second granularity, so two releases built in the same second would tie and
# fall back to (arbitrary) sha ordering — picking the wrong rollback image. Deploy order is
# unambiguous. FAIL SAFE twice over: touch nothing when (a) the live image cannot be identified
# (upstream include missing/unreadable — a transient read must not delete the SERVING image), or
# (b) no previous release has been recorded yet (e.g. the first tick after this change lands),
# since without an authoritative rollback target a heuristic could delete it.
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
# Start a blue-green mcp container at parity with the compose `mcp:` service.
#
# The SSM-materialized values -- MCP_TICKETS_PAT and the four JIRA_* bridge variables -- ride
# `--env-file` below and are deliberately NOT re-spelled as `-e`. `docker run` does not
# interpolate the project `.env`, so `-e JIRA_URL=${JIRA_URL:-}` would expand in THIS shell,
# where the variable is unset, and pass an empty value that OVERRIDES the real one from the
# env-file. They are listed in `_ENV_FILE_ONLY` in tests/scripts/test_autodeploy_mcp_bluegreen.py
# so the parity oracle asserts that exclusion rather than demanding a matching `-e`.
#
# `--label rebar.service=mcp` is the container's STABLE SERVICE IDENTITY (bug
# 9ea3-7d07-ea55-4496). The --name below embeds the deploy SHA and the blue/green port, so
# it is unique per deploy and unusable as a CloudWatch dimension: keyed on it, the
# per-container memory series would restart on every commit and grow custom-metric
# cardinality without bound. compose stamps `com.docker.compose.service` on the containers
# it manages, but this one is a bare `docker run` that compose never sees, so it declares
# what it IS here. observability.sh's §2c census reads the label and never the name.
mcp_run_new() {
  docker run -d --name "$1" \
    --restart always \
    --label rebar.service=mcp \
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
  local live live_img n p img count running
  live="$(mcp_live_port)"
  live_img="$(mcp_image_on_port "$live")"
  while read -r n; do
    [ -n "$n" ] || continue
    p="$(mcp_port_of "$n")"
    running=false
    [ "$(docker inspect -f '{{.State.Running}}' "$n" 2>/dev/null)" = "true" ] && running=true
    # The live-backend guard is UNCONDITIONAL — it is hoisted ABOVE the running/exited branch
    # so it cannot be state-dependent (bug 9ea3). It sat only in the running arm, so a
    # container that WAS the live backend but had crashed fell through to the reap arm below
    # and was `docker rm`'d while $MCP_UPSTREAM_FILE still named its port: `--restart always`
    # then had nothing left to restart, so a TRANSIENT exit became a PERMANENT 502 (the
    # 2026-09-02 state — the include named 8093 with rebar-mcp-…-8093 `Exited (137)`). Do NOT
    # re-add a copy of this test into the else arm; a duplicated guard is the same defect
    # waiting to drift. RELEASES_KEEP is exactly this retention.
    if [ -n "$live" ] && [ "$p" = "$live" ]; then
      # Skipping it is correct but must not be SILENT: the backend nginx points at is DOWN
      # right now, so /mcp is 502ing. Signalled through `err` (AUTODEPLOY_ERROR) — the marker
      # path observability.sh already counts and alarms — rather than a new token, which would
      # need its own offset file and CloudWatch wiring to be visible at all.
      [ "$running" = true ] || err mcp-live-backend-down \
        "live mcp backend $n on port $p is NOT running; /mcp is failing. Retained (never reaped: nginx still points here); awaiting restart/redeploy"
      continue
    fi
    if [ "$running" = true ]; then
      # Never STOP a running container we cannot prove is not the live backend. If the live
      # port is UNKNOWN (upstream include missing/unreadable) fail SAFE: leave every running
      # container alone rather than risk stopping the one still serving /mcp (bug 7b4a). An
      # already-exited container is safe to reap regardless (it serves nothing).
      [ -z "$live" ] && continue
      mcp_retire_graceful "$n"
    else
      # Read the image reference BEFORE the rm — it is unreadable once the container is gone,
      # and the container NAME carries only a truncated sha plus the port, so it cannot be
      # string-manipulated back into a tag. Retire the image only AFTER the reap SUCCEEDS.
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
  # Reconcile leaked orphan tags AFTER the reap loop (reaps first free container refs, so a
  # just-reaped image is removable this same tick) — see mcp_reconcile_orphans (bug e4f3).
  mcp_reconcile_orphans
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

# mcp-scoped mirror of the pair above. Deliberately does NOT exit/skip the tick by itself: it
# only throttles the mcp block, leaving the review-bot path free to deploy on the next tick.
read -r mcp_bo_sha mcp_bo_cnt mcp_bo_next < <(cat "$MCP_BACKOFF_FILE" 2>/dev/null || echo "- 0 0")
[ "$mcp_bo_sha" != "$TARGET" ] && { mcp_bo_cnt=0; }   # new target -> reset (fix-forward)
mcp_backoff_active() { [ "$mcp_bo_sha" = "$TARGET" ] && [ "$(now)" -lt "${mcp_bo_next:-0}" ]; }
# Schedule the mcp backoff throttle (write $MCP_BACKOFF_FILE + prune caches) WITHOUT emitting an
# AUTODEPLOY_ERROR. Shared by the genuine-failure recorder and the routine-backoff variant below:
# both must throttle ticks so they don't hammer during a drain, but ONLY a genuine failure is a
# deploy_error. Sets MCP_BACKOFF_N / MCP_BACKOFF_WAIT for the caller's log/metric line.
schedule_mcp_backoff() {
  MCP_BACKOFF_N=$(( ${mcp_bo_cnt:-0} + 1 ))
  MCP_BACKOFF_WAIT=$(( BACKOFF_BASE * (BACKOFF_FACTOR ** (MCP_BACKOFF_N - 1)) ))
  [ "$MCP_BACKOFF_WAIT" -gt "$BACKOFF_CAP" ] && MCP_BACKOFF_WAIT=$BACKOFF_CAP
  echo "$TARGET $MCP_BACKOFF_N $(( $(now) + MCP_BACKOFF_WAIT ))" > "$MCP_BACKOFF_FILE"
  prune_docker_caches
}
record_mcp_backoff_failure() {
  schedule_mcp_backoff
  # Same metric name as the global recorder so existing deploy_failed alarming still fires;
  # the component= marker is what tells an operator which path failed.
  err deploy_failed "component=mcp target=$TARGET fail#$MCP_BACKOFF_N backoff=${MCP_BACKOFF_WAIT}s"
  log "mcp deploy failed; mcp backoff ${MCP_BACKOFF_WAIT}s (fail #$MCP_BACKOFF_N); OLD mcp upstream stays live, review-bot path unaffected"
}
# Routine (non-error) backoff for the retire-cap / mem-abort paths. These are DOCUMENTED routine
# deferrals that observability.sh deliberately keeps OUT of deploy_errors (their sustained case is
# covered by the dedicated mcp_retire_cap / mcp_mem_abort alarms), so they schedule the SAME
# throttle but must NOT emit AUTODEPLOY_ERROR/deploy_failed and false-page rebar-autodeploy-errors.
record_mcp_routine_backoff() {
  schedule_mcp_backoff
  log "mcp deploy deferred (routine backoff; not a deploy_error); mcp backoff ${MCP_BACKOFF_WAIT}s (fail #$MCP_BACKOFF_N); OLD mcp upstream stays live, review-bot path unaffected"
}
clear_mcp_backoff() { rm -f "$MCP_BACKOFF_FILE"; }

# ── what changed? (computed in the mirror clone) ──────────────────────────────
changed_range() { git -C "$MIRROR_DIR" diff --name-only "$1" "$2" -- $3 2>/dev/null | grep -q .; }
changed() { changed_range "$DEPLOYED" "$TARGET" "$1"; }
# mcp redeploys iff MCP_PATHS **or** SECRETS_PATHS changed over its component sha..target. mcp is
# the OTHER consumer of fetch-secrets output — it starts with `--env-file .env` and a read-only
# bind-mount of mcp-static-tokens.json, both read once at container init — so a secrets-only
# change must reach it exactly as it reaches the review-bot. This mirrors the review-bot gate,
# which has ORed SECRETS_PATHS since f600 / incident 2731 (bug f910). Uses $mcp_deployed and
# $TARGET, both resolved at call time (defined below).
mcp_delta() {
  changed_range "$mcp_deployed" "$TARGET" "$MCP_PATHS" || \
    changed_range "$mcp_deployed" "$TARGET" "$SECRETS_PATHS"
}

# The mcp component's OWN last-deployed sha (see $MCP_SHA_FILE above). Validated like the
# first-run seed: an empty or garbage marker falls back to the global $DEPLOYED rather than
# poisoning the diff range.
mcp_deployed="$(cat "$MCP_SHA_FILE" 2>/dev/null || true)"
if [ -z "$mcp_deployed" ] || ! git -C "$MIRROR_DIR" rev-parse --verify -q "$mcp_deployed^{commit}" >/dev/null 2>&1; then
  mcp_deployed="$DEPLOYED"
fi
bot_deployed="$(cat "$BOT_SHA_FILE" 2>/dev/null || true)"
if [ -z "$bot_deployed" ] || ! git -C "$MIRROR_DIR" rev-parse --verify -q "$bot_deployed^{commit}" >/dev/null 2>&1; then
  bot_deployed="$DEPLOYED"
fi
log "main advanced $DEPLOYED -> $TARGET; computing component deltas"

# ── config refs (replication/g2p/meta): DETECT-ONLY (v1 boundary) ─────────────
if changed "$CONFIG_PATHS"; then
  err config_manual "infra config changed in $TARGET — replication/g2p/refs-meta/gerrit.config need a MANUAL operator apply (auto-apply is a v2 follow-up)"
  log "infra config change detected + signalled (not auto-applied in v1)"
fi

# ── nginx edge (rebar.conf.template): DETECT-ONLY (v1 boundary) ───────────────
if changed "$EDGE_PATHS"; then
  err nginx_edge_manual "nginx edge changed in $TARGET — infra/nginx/rebar.conf.template needs a MANUAL operator render + nginx reload (auto-apply is a v2 follow-up: epic 6d60-2d0c-6ff7-444b)"
  log "nginx edge change detected + signalled (not auto-applied in v1)"
fi

# ── host-nginx materializers (compose-up + materialize-*.sh + seed): DETECT-ONLY ──
if changed "$MATERIALIZER_PATHS"; then
  err nginx_materializer_manual "host-nginx materializer source changed in $TARGET — compose-up.sh / materialize-opcert-guard.sh / materialize-mcp-upstream.sh / mcp-upstream.conf need a MANUAL operator re-materialize + nginx reload (auto-apply is a v2 follow-up: epic 6d60-2d0c-6ff7-444b)"
  log "host-nginx materializer change detected + signalled (not auto-applied in v1)"
fi

# ── review-bot: rebuild + restart ONLY on a source change ─────────────────────
# The body lives in a FUNCTION purely so the drain gate's deferral can leave the BOT path
# (`return`) instead of the whole PROCESS (`exit 0`). It used to `exit 0`, which also
# skipped the INDEPENDENT mcp blue-green block below — and since the bot runs a single
# review worker, `in_flight > 0` is its normal steady state, so in practice ~every tick
# deferred and the mcp service never deployed at all (bug carefree-swift-scallop).
bot_deferred=0
# Set when the bot has a PENDING delta it did not deploy this tick (its backoff window is
# open). Like bot_deferred it means "this tick is not a complete deploy of $TARGET", so the
# footer must not stamp either sha; unlike it, there is no deferral EPISODE to carry forward.
bot_incomplete=0
# The mcp mirror of bot_incomplete: the mcp block had a PENDING delta it did not deploy this
# tick because its own backoff window is open. Same meaning, same consequence — the footer must
# not stamp $TARGET into a component marker for a deploy that never ran.
mcp_incomplete=0
deploy_review_bot() {
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
      # Leave the BOT path only: no rm -f "$DEFER_FILE" (the episode continues), no
      # rebuild/restart, deployed-sha not advanced — but the mcp block below still runs.
      bot_deferred=1
      return 0
    fi
    # Bound exhausted: recreate anyway, but make the kill COUNTABLE. Without this marker the
    # interruption is unobservable — which is the whole reason a fully live-locked gate could
    # sit behind green health signals.
    marker AUTODEPLOY_REVIEW_INTERRUPT bound-exceeded \
      "target=$TARGET in_flight=$inflight deferred_for=${waited}s bound=${DEPLOY_DEFER_MAX}s; recreating anyway, so an in-flight review IS being killed"
    log "deferral bound ${DEPLOY_DEFER_MAX}s exhausted with $inflight review(s) still in flight; proceeding (a review is interrupted; the backfill reconciler retries it)"
  elif [ "$inflight" -lt 0 ]; then
    # The /health in_flight signal is unreadable. Before failing open, tell a bot that is
    # merely MID-REDEPLOY (unreachable because it is itself being recreated / still starting)
    # from one that is genuinely WEDGED. Recreating a mid-redeploy bot would kill the very
    # startup we should wait for and re-enter the same blind window that produced this bug.
    disposition="$(bot_unreachable_disposition)"
    if [ "$disposition" = redeploying ]; then
      # DEFER (retry next tick), bounded EXACTLY like the busy path (same episode + bound) so a
      # bot stuck starting can never freeze the gate. Distinct countable reason so a mid-redeploy
      # deferral is never mistaken for a busy-defer or a fail-open interrupt.
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
    # Fail OPEN on a WEDGED bot (or an exhausted mid-redeploy bound): a bot that cannot answer
    # /health is likely broken or down, and deploying is how a broken bot gets FIXED — blocking
    # deploys on an unparseable field would invent a NEW way to freeze the gate, strictly worse
    # than the bug. But do not let that be silent: a signal that quietly stops working (renamed
    # field, wedged bot) puts us back in the original blind state, so it emits the same countable
    # interrupt marker.
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
  # The bot is DEPLOYED at $TARGET. Record it on the component's OWN marker before any later
  # step can abort the tick, so a subsequent mcp failure cannot cause a needless redeploy.
  echo "$TARGET" > "$BOT_SHA_FILE.tmp" && mv "$BOT_SHA_FILE.tmp" "$BOT_SHA_FILE"
  bot_deployed="$TARGET"
  prune_docker_caches
  log "review-bot redeployed + healthy"
}
# Test the DELTA first, then the backoff: a backoff window is only interesting when there is
# actually something to deploy, and skipping on an open window is only safe to report as a
# no-op tick when there is not. Ordering it the other way logged a backoff skip on ticks with
# no bot work at all, and — the reason this matters — let a tick with REAL pending bot work
# reach the footer with bot_deferred=0, stamping $TARGET into bot-deployed-sha for a deploy
# that never ran. The next tick then saw no pending delta and dropped the change for good.
if changed_range "$bot_deployed" "$TARGET" "$BOT_PATHS" || \
   changed_range "$bot_deployed" "$TARGET" "$SECRETS_PATHS"; then
  if bot_backoff_active; then
    log "review-bot backoff active for $TARGET (fail #$bo_cnt); next bot attempt at $bo_next; the bot delta is still PENDING so deployed-sha will NOT advance"
    bot_incomplete=1
  else
    deploy_review_bot
  fi
fi

# ── mcp: blue-green pointer-swap deploy ONLY on a source change ────────────────
# INDEPENDENT of the review-bot block above: a shared src/rebar / docker-compose.yml change
# triggers both, each on its own path. Unlike the review-bot's stop-and-drain, the mcp server is
# a never-idle shared endpoint, so this does an immutable-release + atomic-pointer-swap cutover
# (start new alongside old -> health -> flip nginx -> retire old when idle) and NEVER kills an
# in-flight certified op. Fatal-on-failure (record_mcp_backoff_failure + exit 1) so deployed-sha
# is not advanced and the deploy retries next tick — on the mcp-SCOPED backoff, so a failure here
# throttles only mcp and never suppresses the review-bot deploy. The gerrit container is never
# touched (blast-radius assert); gerrit is never involved.
if mcp_backoff_active && mcp_delta; then
  log "mcp backoff active for $TARGET (fail #$mcp_bo_cnt); next mcp attempt at $mcp_bo_next; the mcp delta is still PENDING so deployed-sha will NOT advance"
  mcp_incomplete=1
fi
if ! mcp_backoff_active && mcp_delta; then
  log "mcp sources changed $mcp_deployed -> $TARGET; blue-green deploy (blast radius = mcp containers + /mcp nginx upstream only)"
  gerrit_before_mcp="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"

  # 1. sync + build the immutable release image, tagged by the target SHA.
  if ! git -C "$MIRROR_DIR" checkout -q "$TARGET" 2>/dev/null; then
    err mcp-checkout-failed "git checkout $TARGET in $MIRROR_DIR failed"; record_mcp_backoff_failure; exit 1
  fi
  if ! rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$MIRROR_DIR/" "$DEPLOY_REPO/" 2>/dev/null; then
    err mcp-rsync-failed "rsync $MIRROR_DIR -> $DEPLOY_REPO failed"; record_mcp_backoff_failure; exit 1
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
    record_mcp_backoff_failure; exit 1
  fi
  if ! ( cd "$COMPOSE_DIR" && docker compose build mcp ); then
    err mcp-build-failed "docker compose build mcp failed"; record_mcp_backoff_failure; exit 1
  fi
  if ! docker tag "$MCP_IMAGE" "$MCP_IMAGE:$TARGET" >/dev/null 2>&1; then
    err mcp-tag-failed "docker tag $MCP_IMAGE -> $MCP_IMAGE:$TARGET failed"; record_mcp_backoff_failure; exit 1
  fi

  # 2. memory pre-check BEFORE the 2x overlap (8 GiB box). Below the floor: ABORT before ever
  #    starting the second container. Unreadable (-1) fails OPEN.
  mcp_mem="$(mcp_mem_available_mb)"
  if [ "$mcp_mem" -ge 0 ] && [ "$mcp_mem" -lt "$MCP_MEM_MIN_MB" ]; then
    marker AUTODEPLOY_MCP_MEM_ABORT low-memory "MemAvailable=${mcp_mem}MB < min ${MCP_MEM_MIN_MB}MB on the 8GiB box; refusing the blue-green 2x overlap"
    record_mcp_routine_backoff; exit 1
  fi
  [ "$mcp_mem" -lt 0 ] && log "mcp mem-check: MemAvailable UNREADABLE; failing OPEN (proceeding with the 2x overlap without a memory guarantee)"

  # 3. pick a FREE blue/green host port. Both busy -> emit the cap marker + back off, start
  #    NOTHING (never collide on a live port; managed set is capped at the {8091,A,B} pool).
  mcp_newport="$(mcp_free_port)"
  if [ -z "$mcp_newport" ]; then
    marker AUTODEPLOY_MCP_RETIRE_CAP port-exhausted "both $MCP_PORT_A and $MCP_PORT_B held by un-reaped mcp containers; not starting a colliding 3rd (cap=$MCP_RELEASES_CAP)"
    record_mcp_routine_backoff; exit 1
  fi
  mcp_newname="${MCP_CONTAINER_PREFIX}-${TARGET:0:12}-${mcp_newport}"

  # 4. start the NEW container ALONGSIDE the old.
  if ! mcp_run_new "$mcp_newname" "$mcp_newport"; then
    err mcp-run-failed "docker run $mcp_newname on 127.0.0.1:${mcp_newport} failed"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    record_mcp_backoff_failure; exit 1
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
    record_mcp_backoff_failure; exit 1
  fi

  # 5b. A 200 from /health used to mean only "the process is up". A container serving NO ticket
  #     store passed this gate identically to a healthy one, which is how a storeless mcp
  #     deployment went unobserved for weeks (mobile-groovy-badger). /health now reports
  #     {"store": {"present": ..., "expected": ...}}; refuse to promote a container that was
  #     SUPPOSED to have a store and does not.
  #
  #     Gated on `expected`, NOT on `present` alone, and that is the whole point: a deployment
  #     that never configured a tracker dir legitimately has no store, so requiring one
  #     unconditionally would refuse to promote a perfectly good container and take the endpoint
  #     down to report a non-problem. Keying on `expected` makes this INERT until a tracker dir
  #     is configured and strict the moment one is — no flag day. An older container whose
  #     /health predates the `store` field reports neither key and is treated as not-expected,
  #     so a mixed-version deploy cannot be blocked by this.
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
    # Records the MCP backoff, NOT $BACKOFF_FILE (which gates the whole script and would throttle
    # a review-bot deploy that never ran this tick — see MCP_BACKOFF_FILE, and the sibling
    # mcp-run / mcp-unhealthy / mcp-handshake / mcp-flip branches which all record it too).
    record_mcp_backoff_failure; exit 1
  fi

  # 5c. A 200 from /health still does not prove this container can serve an MCP REQUEST. The
  #     route is a custom_route registered OUTSIDE the auth middleware and the DNS-rebinding
  #     transport-security guard, so a container whose allowlist does not admit the hostname real
  #     traffic carries answers /health 200 while returning 421 to every /mcp request — and this
  #     gate promoted it (vaccinated-flavorous-solenodon; measured: promoted 54 s before the
  #     container served its first /mcp request). The server now drives one real `initialize`
  #     through its OWN session manager inside ASGI lifespan startup, before uvicorn accepts a
  #     connection, and reports {"handshake": {"ok": ...}}.
  #
  #     Absent field => promote, exactly as the `store` gate treats an older container: a
  #     mixed-version deploy must not be blocked by a field that container cannot report.
  #     Failure records the MCP backoff, NOT $BACKOFF_FILE (which gates the whole script and
  #     would throttle a review-bot deploy that never ran this tick — see MCP_BACKOFF_FILE).
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


  # 6. ATOMICALLY flip the /mcp/ upstream to the new container. The cutover is DONE at the reload;
  #    it never waits on the OLD backend's in-flight ops. On failure restore the previous include
  #    (byte-identical) + remove the new container + back off.
  if ! mcp_flip_upstream "$mcp_newport"; then
    err mcp-flip-failed "nginx flip to 127.0.0.1:${mcp_newport} failed; restored previous upstream + removing new container"
    docker rm -f "$mcp_newname" >/dev/null 2>&1 || true
    record_mcp_backoff_failure; exit 1
  fi
  log "mcp cutover complete: /mcp upstream now 127.0.0.1:${mcp_newport} (deploy DONE; not waiting on in-flight drain)"

  # Record the OUTGOING release as the rollback target BEFORE the retire sweep runs, so the
  # orphan reconcile inside it preserves exactly {new live, this previous}. $mcp_deployed still
  # holds the sha that was live until the flip above; a valid, distinct 40-hex sha is the
  # immediately-previous release (empty on the very first mcp deploy — nothing to roll back to).
  case "$mcp_deployed" in
    *[!0-9a-f]*|"") : ;;
    *) if [ "${#mcp_deployed}" -eq 40 ] && [ "$mcp_deployed" != "$TARGET" ]; then
         echo "$mcp_deployed" > "$MCP_PREV_SHA_FILE.tmp" && mv "$MCP_PREV_SHA_FILE.tmp" "$MCP_PREV_SHA_FILE"
         log "mcp reconcile: recorded previous release $mcp_deployed (rollback target; preserved by orphan sweep)"
       fi ;;
  esac

  # 7. retire the OLD backend off the critical path (graceful docker stop, reap when drained).
  mcp_retire_sweep

  # blast-radius assertion: the gerrit container must be UNTOUCHED.
  gerrit_after_mcp="$(docker inspect -f '{{.Id}}' "$GERRIT_CONTAINER" 2>/dev/null || true)"
  if [ -n "$gerrit_before_mcp" ] && [ "$gerrit_before_mcp" != "$gerrit_after_mcp" ]; then
    err blast-radius "gerrit container id changed during an mcp deploy — investigate"
  fi
  # mcp is DEPLOYED at $TARGET. Record it on the component's OWN marker (atomically, same
  # idiom as the deployed-sha advance below) so a tick that exits early at the bot_deferred
  # guard — never reaching that advance — does not re-deploy this identical mcp delta forever.
  echo "$TARGET" > "$MCP_SHA_FILE.tmp" && mv "$MCP_SHA_FILE.tmp" "$MCP_SHA_FILE"
  mcp_deployed="$TARGET"
  clear_mcp_backoff
  prune_docker_caches
  log "mcp redeployed + healthy at 127.0.0.1:${mcp_newport}"
fi

# ── review-bot did not deploy: stop HERE, AFTER the independent mcp path ───────
# Either the bot deploy was DEFERRED (a review is in flight) or it was SKIPPED with a delta
# still pending (its backoff window is open). Both mean the tick is NOT a complete deploy of
# $TARGET: neither sha may advance, and on the deferral path the EPISODE in $DEFER_FILE must
# carry forward. Everything below (host probe / certbot re-materialization, the deployed-sha
# advance) is skipped exactly as the pre-fix `exit 0` skipped it — the ONLY behavioural
# difference is that the mcp blue-green block above already ran on its own path.
if [ "$bot_deferred" = 1 ] || [ "$bot_incomplete" = 1 ] || [ "$mcp_incomplete" = 1 ]; then
  exit 0
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
# Keep the component markers in lockstep with the global one on a COMPLETE deploy. Either block
# may have been a NO-OP this tick — because nothing under its trigger paths changed, or because
# its backoff window was open with a real delta still pending. Only the FORMER is a complete
# tick; the latter sets bot_incomplete / mcp_incomplete and exits at the guard above, so
# reaching HERE means every component is genuinely at $TARGET and stamping both markers is
# sound. Otherwise a later tick would diff from a stale marker, compute no delta, and drop the
# pending change permanently.
echo "$TARGET" > "$MCP_SHA_FILE.tmp" && mv "$MCP_SHA_FILE.tmp" "$MCP_SHA_FILE"
echo "$TARGET" > "$BOT_SHA_FILE.tmp" && mv "$BOT_SHA_FILE.tmp" "$BOT_SHA_FILE"
clear_backoff
clear_mcp_backoff
rm -f "$DEFER_FILE"          # the deferral episode ended with a deploy; do not carry it forward
log "deploy complete: env now reflects $TARGET"
