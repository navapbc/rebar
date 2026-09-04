#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# observability.sh — the rebar box's host observability probe (S2 + S5 + S4b + S7 + 1fa8).
#
# Run periodically by a systemd timer (install-observability.sh). Each run publishes
# CloudWatch metrics + journald log lines:
#   1. Health probe of Gerrit + the review-bot (/review/health) -> journald +
#      rebar/host:{gerrit_healthy,reviewbot_healthy} (S2).
#   1b. Health probe of the rebar MCP SERVING PATH (https://<domain>/mcp through nginx and
#      the materialized `upstream rebar_mcp` include) -> rebar/host:mcp_healthy, a 1/0
#      heartbeat published on every tick (bug 9ea3-7d07-ea55-4496; alarm in monitoring_9ea3.tf).
#   2. Gerrit data-volume disk-used-percent -> rebar/host:disk_used_percent (S2 alarm).
#   2c. Non-`site/` debris on the Gerrit data volume (bytes under /var/gerrit that are not
#       the Gerrit site tree) -> rebar/host:data_disk_debris_bytes (task 3e92 alarm). Answers
#       "full OF WHAT", which the used-percent reading in 2 structurally cannot.
#   2d. Host memory (mem_available_percent / mem_used_percent / mem_probe_ok) and
#       per-container resident set (container_memory_rss_bytes, `container` dimension, plus
#       the container_stats_ok census heartbeat) -> rebar/host (bug 9ea3; measurement only,
#       no alarm yet).
#   3. Gerrit->GitHub replication failures (replication_log) -> rebar/host:replication_errors (S5 alarm).
#   4. review-bot voter failures (VOTER_ERROR in journald) -> rebar/host:voter_errors (S4b alarm).
#   4c. review-bot merge-change failures (MERGE_CHANGE_ERROR) -> rebar/host:review_bot_merge_change_errors (epic 88ab/S2 alarm).
#   4d. continuous auto-deploy failures (AUTODEPLOY_ERROR in the unit journal) -> rebar/host:deploy_errors (epic 88ab/8903 alarm).
#   4e. auto-deploys DEFERRED to avoid killing an in-flight review (AUTODEPLOY_DEFERRED) ->
#       rebar/host:deploy_deferrals, and deploys that recreated the container ANYWAY
#       (AUTODEPLOY_REVIEW_INTERRUPT) -> rebar/host:review_interrupts (bug 34cd alarm), and
#       pressure-triggered reclaims on the no-op tick (AUTODEPLOY_DISK_PRESSURE) ->
#       rebar/host:disk_pressure_prunes (diagnostic counter, task 9d15 — no alarm), and
#       reclaims that ran and did NOT recover the disk (AUTODEPLOY_DISK_PRESSURE_PERSISTS) ->
#       rebar/host:disk_pressure_persists (bug 9bc0 — alarmable "reclaim is ineffective").
#   4f. mcp blue-green target (foxterrier): retire/port-pool cap hits (AUTODEPLOY_MCP_RETIRE_CAP)
#       -> rebar/host:mcp_retire_cap, and low-memory deploy aborts (AUTODEPLOY_MCP_MEM_ABORT) ->
#       rebar/host:mcp_mem_abort (both alarmed in monitoring_foxterrier.tf).
#   4b. gerrit-to-platform CI-dispatch failures (Gerrit journald) -> rebar/host:g2p_dispatch_errors (epic 1fa8 alarm).
#   5. Gate reachability -> Rebar/Gate:GerritReachable (1/0), watched by the S7 gate-down
#      alarm (treat_missing_data=breaching catches a dead host / stopped probe).
#   6. Gerrit->GitHub mirror out-of-sync -> rebar/host:mirror_out_of_sync (WS7/a774 alarm).
#
# Auth: the EC2 instance role (S1) grants cloudwatch:PutMetricData. No static keys.
# ---------------------------------------------------------------------------
set -uo pipefail

DOMAIN="${DOMAIN:-rebar.solutions.navateam.com}"
DATA_MOUNT="${DATA_MOUNT:-/var/gerrit}"
# Where the dedicated review-gate scratch volume mounts (ADR 0112 decision 3, story aa40).
# Overridable for the tests exactly as DATA_MOUNT is; the production default is the
# terraform `gate_scratch_mount` variable and must stay in step with it.
GATE_SCRATCH_MOUNT="${GATE_SCRATCH_MOUNT:-/var/lib/rebar/gate-scratch}"
NS="rebar/host"

# IMDSv2 region.
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 120')
REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region \
  -H "X-aws-ec2-metadata-token: $TOKEN")
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id \
  -H "X-aws-ec2-metadata-token: $TOKEN")

# --- 1. Health probes ------------------------------------------------------
gerrit_code=$(curl -sS -o /dev/null -w '%{http_code}' "https://${DOMAIN}/config/server/version" --max-time 10 2>/dev/null || echo 000)
review_code=$(curl -sS -o /dev/null -w '%{http_code}' "https://${DOMAIN}/review/health" --max-time 10 2>/dev/null || echo 000)
logger -t rebar-health "gerrit=/config/server/version:${gerrit_code} review-bot=/review/health:${review_code}"

# Publish health as a metric too (1=ok, 0=bad) for alarming if desired.
gerrit_ok=0; [ "$gerrit_code" = "200" ] && gerrit_ok=1
review_ok=0; [ "$review_code" = "200" ] && review_ok=1
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name gerrit_healthy --unit Count --value "$gerrit_ok" \
  --dimensions InstanceId="$IID" 2>/dev/null || true
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name reviewbot_healthy --unit Count --value "$review_ok" \
  --dimensions InstanceId="$IID" 2>/dev/null || true

# Gate-reachable signal for the S7 gerrit-gate-down alarm. Reuses the SAME
# gerrit_ok value computed above (1 if the /config/server/version probe returned
# 200, else 0) but publishes it to a SEPARATE namespace WITHOUT dimensions.
# DIMENSIONLESS ON BOTH SIDES: the S7 alarm (monitoring.tf, Rebar/Gate /
# GerritReachable) declares no dimensions, and CloudWatch keys a metric by
# namespace+name+dimensions — adding a dimension to only one side makes the alarm
# silently stop matching. When the host/probe stops publishing entirely the alarm's
# treat_missing_data=breaching turns that gap into an ALARM (host-down backstop).
aws cloudwatch put-metric-data --region "$REGION" --namespace "Rebar/Gate" \
  --metric-name GerritReachable --unit Count --value "$gerrit_ok" 2>/dev/null || true

# --- 1b. rebar MCP serving-path health (bug 9ea3-7d07-ea55-4496) -----------
# WHAT THIS WATCHES, AND WHY IT IS THE EDGE AND NOT THE CONTAINER. On 2026-09-02 the
# mcp container was OOM-killed mid-gate (docker inspect: OOMKilled=true, Exit=137).
# nginx stayed up and healthy, but `upstream rebar_mcp` is a SINGLE materialized
# `server 127.0.0.1:<port>;` line with no failover, so it kept pointing at the dead
# container and every /mcp request 502'd for ~12 hours until a HUMAN reported it.
# Nothing on the box could see that: gerrit_healthy and reviewbot_healthy were both 1,
# and the only mcp metrics that existed (mcp_retire_cap, mcp_mem_abort, §4f) are
# DEPLOY-PATH markers — they read 0 throughout, because a kernel OOM-kill of an
# already-deployed container is not a deploy event.
#
# So this probes the SERVING PATH — the exact URL a client uses, through TLS, through
# nginx, through the materialized upstream include — rather than the container's own
# /health on the loopback port. The loopback probe would answer the narrower question
# "does an mcp container exist somewhere", which is not the question the outage asked:
# the outage was a BINDING failure between a healthy edge and a dead backend, and only
# an end-to-end request crosses that binding. (autodeploy.sh §5b does probe the
# loopback /health, deliberately — it is gating a CANDIDATE container before the flip,
# which is a different question with a different right answer.)
#
# 401 IS THE HEALTHY CODE — "2xx == healthy" WOULD BE WRONG HERE. /mcp requires a
# bearer PAT (docs/mcp-auth.md), so an unauthenticated GET from this probe is answered
# 401 by the app's own auth middleware. That makes 401 an unambiguous liveness proof:
# nginx never synthesises a 401 for this location (it has no auth_basic and no
# auth_request), so the code can only have come from the mcp application itself,
# reached through the live upstream. Every other outcome is unhealthy, and the
# outage's signature is among them: 502/503/504 (nginx has no live backend), 000
# (TLS/DNS/timeout — curl also prints 000 and exits non-zero, which the `|| echo 000`
# turns into a non-401 string either way), and 404 (the upstream/location binding was
# lost and the request fell through to Gerrit). We deliberately do NOT widen the
# healthy set to "any non-5xx": a 200 here would mean the auth middleware is NOT
# running, which is itself worth paging for.
#
# HEARTBEAT, NOT AN EVENT (ticket bff5-9163-cddd-4158): a value is published on EVERY
# tick, including the unhealthy and probe-failed paths, so the metric is continuously
# present and its ABSENCE means the probe/timer/host is dead — which is what the
# alarm's treat_missing_data = "breaching" (monitoring_9ea3.tf) then catches. Publishing
# nothing on the bad path would leave the alarm with no datapoint to evaluate and is the
# exact fail-open bff5 removed.
#
# DIMENSIONLESS on both sides, unlike gerrit_healthy/reviewbot_healthy above: the alarm
# in monitoring_9ea3.tf declares no dimensions, and CloudWatch keys a metric by
# namespace+name+dimensions, so a dimension on one side only silently unmatches.
mcp_code=$(curl -sS -o /dev/null -w '%{http_code}' "https://${DOMAIN}/mcp" --max-time 10 2>/dev/null || echo 000)
mcp_ok=0; [ "$mcp_code" = "401" ] && mcp_ok=1
logger -t rebar-health "mcp=/mcp:${mcp_code} mcp_healthy=${mcp_ok}"
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name mcp_healthy --unit Count --value "$mcp_ok" 2>/dev/null || true
[ "$mcp_ok" -eq 0 ] && logger -t rebar-health \
  "mcp serving path UNHEALTHY: https://${DOMAIN}/mcp returned '${mcp_code}' (expected 401); check the live rebar-mcp container and the materialized /etc/nginx/mcp-upstream.conf"

# --- 2. Disk usage of the Gerrit data volume -------------------------------
used_pct=$(df --output=pcent "$DATA_MOUNT" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$used_pct" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name disk_used_percent --unit Percent --value "$used_pct" \
    --dimensions InstanceId="$IID",mount="$DATA_MOUNT" 2>/dev/null || true
  logger -t rebar-health "disk ${DATA_MOUNT} used_percent=${used_pct}"
fi

# --- 2b. ROOT filesystem usage (incident 2731) ------------------------------
# The 30G root disk holds docker's image/build-cache storage and the review-bot
# clone tmp; when it filled, every LLM-Review fail-closed (ENOSPC) with no metric
# even watching it. DIMENSIONLESS on both sides (the GerritReachable convention):
# the rebar-root-disk-pressure alarm (monitoring_autodeploy.tf) declares no
# dimensions, and CloudWatch keys metrics by namespace+name+dimensions.
root_pct=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$root_pct" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name root_disk_used_percent --unit Percent --value "$root_pct" 2>/dev/null || true
  logger -t rebar-health "disk / used_percent=${root_pct}"
fi

# --- 2e. Review-gate SCRATCH volume (ADR 0112 decision 3, story aa40) --------
# Gate snapshots and the review-bot's per-review clones moved off root onto their own
# EBS volume, so root pressure no longer answers for them and they need their own
# reading. TWO metrics, because they answer different questions and the second is not
# implied by the first: a volume that failed to mount reads 0% used — indistinguishable
# from healthy — while every gate on the box refuses.
#
# gate_scratch_mounted is a HEARTBEAT (ticket bff5): a value is published on EVERY tick,
# including the unmounted path, so its ABSENCE means the probe/timer/host is dead rather
# than "the volume was fine". Mountedness is decided by the SAME proof marker rebar's
# gate admission reads — a file that lives ON the volume, so it disappears with it —
# rather than by `mountpoint`, so the probe and the refusal cannot disagree about the
# state of the same volume. DIMENSIONLESS, following root_disk_used_percent (2b) and the
# dimensionless alarm in monitoring_autodeploy.tf.
scratch_mounted=0
[ -f "$GATE_SCRATCH_MOUNT/.gate-scratch-mounted" ] && scratch_mounted=1
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name gate_scratch_mounted --unit Count --value "$scratch_mounted" 2>/dev/null || true
logger -t rebar-health "gate scratch ${GATE_SCRATCH_MOUNT} mounted=${scratch_mounted}"
if [ "$scratch_mounted" -eq 0 ]; then
  logger -t rebar-health \
    "gate scratch volume ${GATE_SCRATCH_MOUNT} is NOT mounted — rebar gate admission refuses every plan-review and completion-verifier run rather than writing to the ROOT filesystem; see infra/runbooks/review-bot-ops.md"
fi

# Used-percent follows 2's convention: a READING, not a delta, published only when df
# actually reported one. Dimensioned InstanceId+mount like disk_used_percent for the data
# volume, so one metric name serves every mount and the alarm selects with `mount`.
scratch_pct=$(df --output=pcent "$GATE_SCRATCH_MOUNT" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$scratch_pct" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name disk_used_percent --unit Percent --value "$scratch_pct" \
    --dimensions InstanceId="$IID",mount="$GATE_SCRATCH_MOUNT" 2>/dev/null || true
  logger -t rebar-health "disk ${GATE_SCRATCH_MOUNT} used_percent=${scratch_pct}"
fi

# --- 2c. Non-`site/` debris on the Gerrit DATA volume (task 3e92) ------------
# §2 above answers "how full is /var/gerrit"; it cannot answer "full OF WHAT". The
# 2026-08-26 disk-fill was 65% one-off investigation evidence —
# /var/gerrit/rebar-quiet-window-evidence/ held two ~5.2G epoch-probe dumps — written by
# ad-hoc operator/agent shell, not by any rebar process. Nothing observed that until a
# human ran `du` during the incident, so the fill read as ordinary growth of the git
# repos it was not.
#
# This census is the DETECTION half of that remediation and the only enforceable half:
# rebar cannot stop a shell on the box from writing wherever it likes, so the guard is
# that such a write becomes VISIBLE within one probe interval instead of accumulating
# silently. The policy half — where evidence is supposed to go — is documented in
# infra/runbooks/gerrit-data-volume-reclaim.md and is advisory.
#
# EVERYTHING that is not the Gerrit site tree is debris by definition. The allow-list is
# deliberately tiny (`site` — the Gerrit data root compose-up.sh binds — plus the
# filesystem's own `lost+found`) and NOT extended per incident: a legitimate new
# top-level consumer of the data volume is itself a decision worth paging about once.
# DATA_DEBRIS_ALLOW is the seam the tests drive; it is not a production tuning knob.
#
# Publishes a READING, not a delta, so it follows §2's honesty rule rather than the
# offset-counter convention: when $DATA_MOUNT is not a directory nothing is published,
# because 0 would assert a clean volume we did not observe. The alarm
# (rebar-gerrit-data-disk-debris, monitoring.tf) is treat_missing_data = "breaching", so
# that silence pages exactly like a dead publisher.
DATA_DEBRIS_ALLOW="${DATA_DEBRIS_ALLOW:-site lost+found}"
if [ -d "$DATA_MOUNT" ]; then
  debris_bytes=0
  debris_names=""
  for entry in "$DATA_MOUNT"/* "$DATA_MOUNT"/.[!.]*; do
    [ -e "$entry" ] || continue   # unmatched glob stays literal; skip it
    name=${entry##*/}
    allowed=0
    for keep in $DATA_DEBRIS_ALLOW; do
      if [ "$name" = "$keep" ]; then allowed=1; break; fi
    done
    [ "$allowed" -eq 1 ] && continue
    # `du -sk` (not -sb): -b is GNU-only and this must also run under the macOS du the
    # test suite invokes. KiB * 1024 is exact for the sizes involved.
    entry_kb=$(du -sk "$entry" 2>/dev/null | tail -1 | awk '{print $1}')
    entry_kb=${entry_kb:-0}
    debris_bytes=$((debris_bytes + entry_kb * 1024))
    debris_names="${debris_names} ${name}"
  done
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name data_disk_debris_bytes --unit Bytes --value "$debris_bytes" \
    --dimensions InstanceId="$IID",mount="$DATA_MOUNT" 2>/dev/null || true
  logger -t rebar-health \
    "disk ${DATA_MOUNT} debris_bytes=${debris_bytes} entries=[${debris_names# }]"
  if [ "$debris_bytes" -gt 0 ]; then
    logger -t rebar-health \
      "non-site debris on the Gerrit DATA volume ${DATA_MOUNT}:${debris_names} — investigation output does not belong here; see infra/runbooks/gerrit-data-volume-reclaim.md"
  fi
fi

# --- 2d. HOST MEMORY + per-container RSS (bug 9ea3-7d07-ea55-4496) ----------
# MEMORY WAS ENTIRELY UNMETERED ON THIS BOX. The mcp container was OOM-killed ~3
# minutes into a plan-review gate run (docker inspect: OOMKilled=true, Exit=137) on a
# t4g.large (8 GiB) it shares with Gerrit — which alone reserves ~3 GiB by config
# (gerrit.config heapLimit + container_heap_limit) — the review-bot, and opcert. NO
# container declares a memory limit (docker-compose.yml has neither `mem_limit` nor
# `deploy.resources.limits`), and this probe published disk and health but not one
# byte of memory. The only memory check anywhere was autodeploy.sh's MCP_MEM_MIN_MB
# (default 1024), an UNDERIVED fail-open deploy-time floor.
#
# So the box was sized, and its deploy floor set, on a guess. This section exists to
# replace that guess with data. It is DELIBERATELY MEASUREMENT-ONLY: no alarm watches
# these metrics yet and no limit is derived from them here, because any threshold
# chosen today would be another guess. Measure first, choose limits later.
#
# HEARTBEAT, NOT AN EVENT (ticket bff5-9163-cddd-4158). The host gauges publish on
# EVERY tick, including the paths where the read fails, so their ABSENCE means the
# probe/timer/host is dead rather than "memory was fine". A failed read publishes the
# PESSIMISTIC value in each metric's own direction (0% available / 100% used), the same
# convention §5's mirror_out_of_sync uses when its comparison cannot be made — and
# `mem_probe_ok` is published alongside precisely so a synthesised pessimistic reading is
# never mistaken for a measured one. A future alarm must gate on `mem_probe_ok`, and any
# ANALYSIS of this data must drop the ticks where it is 0.
#
# DIMENSIONS. The host gauges are DIMENSIONLESS, following root_disk_used_percent (§2b)
# and the GerritReachable convention: every rebar/host alarm in monitoring*.tf declares
# no dimensions, and CloudWatch keys a metric by namespace+name+dimensions, so a
# dimension added on only one side silently unmatches. The per-container gauge instead
# follows the OTHER local precedent — disk_used_percent's `mount` dimension (§2) — and
# publishes ONE metric name carrying a `service` dimension rather than a metric name
# per service. That is what the question needs: "which resident set grew" is a
# comparison ACROSS services, which a dimension makes a single graph/`Max by service`
# query, whereas per-service metric names would need this probe (and every consumer)
# edited each time compose gains or renames a service. The dimension VALUE is a stable
# service identity read from a container label, never the container name — see the block
# below for why a name would be unbounded. InstanceId rides along exactly as it does on
# disk_used_percent.

# Host memory. `free -k` reports KiB; column 7 of the Mem: row is `available` (what a new
# allocation can actually get, accounting for reclaimable page cache) which is the number
# an OOM-kill is about — NOT `free` (column 4), which reads alarmingly low on any healthy
# box with a warm cache. Very old procps has no `available` column, so fall back to
# free+buff/cache there. A missing/failed `free` leaves both empty and takes the
# pessimistic branch below.
mem_avail_pct=""
mem_used_pct=""
mem_stats=$(free -k 2>/dev/null | awk '/^Mem:/ {
  total = $2
  if (total <= 0) exit
  avail = (NF >= 7) ? $7 : $4 + $6
  printf "%d %d", (avail * 100) / total, ((total - avail) * 100) / total
  exit
}') || true
case "$mem_stats" in
  *[0-9]" "[0-9]*)
    mem_avail_pct=${mem_stats%% *}
    mem_used_pct=${mem_stats##* }
    ;;
esac
mem_probe_ok=1
if [ -z "$mem_avail_pct" ] || [ -z "$mem_used_pct" ]; then
  # Publish rather than fall silent, in each gauge's pessimistic direction, and say so.
  mem_probe_ok=0
  mem_avail_pct=0
  mem_used_pct=100
  logger -t rebar-health "memory probe FAILED (free unavailable or unparseable); published pessimistic mem_available_percent=0 mem_used_percent=100 with mem_probe_ok=0"
fi
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name mem_available_percent --unit Percent --value "$mem_avail_pct" 2>/dev/null || true
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name mem_used_percent --unit Percent --value "$mem_used_pct" 2>/dev/null || true
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name mem_probe_ok --unit Count --value "$mem_probe_ok" 2>/dev/null || true
[ "$mem_probe_ok" -eq 1 ] && logger -t rebar-health \
  "memory available_percent=${mem_avail_pct} used_percent=${mem_used_pct}"

# Per-container resident set. BOUNDED TWICE, because the docker calls here are the ones
# that can WEDGE THE 5-MINUTE TIMER: `docker stats` streams by default, and on a loaded or
# memory-pressured box (exactly the condition this metric exists to catch) either call can
# block on the daemon indefinitely. `--no-stream` makes stats a single sample instead of a
# stream, and `timeout 15` caps the wall clock on BOTH regardless — the same
# `timeout N docker …` idiom autodeploy.sh already uses for `docker compose logs` and the
# prune calls, which is the established precedent for bounding docker on this host. If
# `timeout` itself is missing, the commands simply fail and the ok-gauge below reports 0;
# they can never hang.
#
# THE DIMENSION IS A STABLE SERVICE IDENTITY, NEVER THE CONTAINER NAME. autodeploy's
# blue-green mcp containers are named "${MCP_CONTAINER_PREFIX}-${TARGET:0:12}-${port}"
# (observed on the box: rebar-mcp-3e04025b684e-8092, rebar-mcp-8db373933654-8093), so
# EVERY DEPLOY MINTS A NEW NAME. Keying the CloudWatch dimension on that name would grow
# custom-metric cardinality without bound — CloudWatch bills per unique dimension
# combination — and, fatally for the campaign this section exists to run, RESTART THE
# SERIES AT EVERY DEPLOY: "peak mcp RSS during a gate" is a comparison across gate runs,
# and a series that resets whenever a commit lands cannot answer it.
#
# The identity comes from a LABEL, not from parsing the name. A regex over a naming
# convention re-breaks the moment the convention changes, silently and in the direction of
# MORE cardinality (an unmatched name falls through as itself), which is the failure this
# is guarding against. `docker stats --format` cannot emit labels, so a second bounded
# `docker ps` supplies the name->service map and the awk below joins the two streams:
#   - `com.docker.compose.service` already labels every compose-managed container on the
#     box (gerrit, review-bot, opcert, and the boot mcp backend compose-mcp-1). Nothing to
#     add there — compose stamps it, and its value IS the service name.
#   - the blue-green mcp containers come from a bare `docker run` in autodeploy.sh
#     (mcp_run_new), which compose never sees and never labels, so autodeploy stamps
#     `rebar.service=mcp` on them. That is the one place the unbounded name is minted, so
#     it is the one place that has to declare what the container IS.
# `rebar.service` wins where both are present, so an explicit stamp can always override a
# compose default. A container carrying NEITHER label — an mcp container that predates the
# stamp, or something hand-run — is bucketed under the single constant `unlabeled` rather
# than under its own name: bounded by construction, obvious in the data, and self-healing
# at the next deploy. Its raw name still reaches journald, where cardinality is free.
#
# During a blue-green cutover two mcp containers are briefly live and both publish to
# service=mcp in the same tick. That is intended: CloudWatch keeps both datapoints, and
# `Maximum` — the statistic a peak-RSS question asks for — reads the larger of them.
#
# MemUsage is human-formatted ("1.234GiB / 7.664GiB"), so the awk converts the used side
# to bytes. Docker emits binary units (B/KiB/MiB/GiB/TiB); the decimal spellings are
# accepted too so a docker version that prints them is not silently dropped.
#
# AN UNPARSEABLE ROW IS COUNTED, NOT SWALLOWED. A row whose unit or figure the awk does not
# recognise used to `next` in silence while `container_stats_ok` still went to 1 off any
# other row — a container present in `docker stats` vanished from the data behind a flag
# that said everything was observed. That is the same defect class this whole section was
# written to remove, so the drops are published as their own count
# (`container_stats_unparsed_rows`) and named individually in journald. It is a SEPARATE
# metric rather than a failure of `container_stats_ok` deliberately: that gauge answers
# "did the census run at all", which is what distinguishes a wedged daemon from an idle
# box, and folding row-level parse quality into it would make one weird row
# indistinguishable from a dead docker. Both signals are published on every tick,
# including 0, so their absence still means the probe is dead.
container_stats_ok=0
container_unparsed=0
container_ps=$(timeout 15 docker ps --no-trunc \
  --format 'PS|{{.Names}}|{{.Label "rebar.service"}}|{{.Label "com.docker.compose.service"}}' \
  2>/dev/null) || true
container_stats=$(timeout 15 docker stats --no-stream \
  --format 'ST|{{.Name}}|{{.MemUsage}}' 2>/dev/null) || true
container_census=$(printf '%s\n%s\n' "$container_ps" "$container_stats" | awk -F'|' '
  $1 == "PS" {
    service[$2] = ($3 != "") ? $3 : $4
    next
  }
  $1 == "ST" {
    split($3, used, " ")
    value = used[1]
    unit = value
    gsub(/[0-9.]/, "", unit)
    figure = value
    gsub(/[^0-9.]/, "", figure)
    mult = 1
    if (unit == "KiB" || unit == "kB" || unit == "KB") mult = 1024
    else if (unit == "MiB" || unit == "MB") mult = 1048576
    else if (unit == "GiB" || unit == "GB") mult = 1073741824
    else if (unit == "TiB" || unit == "TB") mult = 1099511627776
    else if (unit != "B" && unit != "") { print "DROP " $2 " " $3; next }
    # An absent or non-numeric figure is NOT a zero-byte container. Publishing 0 for it
    # would be the same silent lie as dropping the row, so it is a drop that gets counted.
    if (figure !~ /^[0-9]+(\.[0-9]+)?$/) { print "DROP " $2 " " $3; next }
    printf "ROW %s %.0f %s\n", (service[$2] != "" ? service[$2] : "unlabeled"), \
      (figure + 0) * mult, ($2 == "" ? "<unnamed>" : $2)
  }') || true
while read -r kind field_a field_b field_rest; do
  case "$kind" in
    ROW)
      container_stats_ok=1
      aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
        --metric-name container_memory_rss_bytes --unit Bytes --value "$field_b" \
        --dimensions InstanceId="$IID",service="$field_a" 2>/dev/null || true
      logger -t rebar-health \
        "container ${field_rest} service=${field_a} memory_rss_bytes=${field_b}"
      if [ "$field_a" = "unlabeled" ]; then
        logger -t rebar-health "container ${field_rest} carries neither rebar.service nor com.docker.compose.service; bucketed as service=unlabeled (its raw name is recorded here, never as a metric dimension)"
      fi
      ;;
    DROP)
      container_unparsed=$((container_unparsed + 1))
      logger -t rebar-health \
        "container ${field_a} memory row UNPARSEABLE (\"${field_b} ${field_rest}\"); dropped from the census and counted in container_stats_unparsed_rows"
      ;;
  esac
done <<EOF
$container_census
EOF
# The census's own heartbeat: WITHOUT it, "docker stats timed out / the daemon is wedged"
# and "every container is stopped" are the same observation (no per-container datapoints
# at all), and the per-container gauge cannot carry a heartbeat of its own because its
# dimension set is only knowable from a census that succeeded.
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name container_stats_ok --unit Count --value "$container_stats_ok" 2>/dev/null || true
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name container_stats_unparsed_rows --unit Count --value "$container_unparsed" 2>/dev/null || true
[ "$container_stats_ok" -eq 0 ] && logger -t rebar-health \
  "container memory census produced no rows (docker stats failed, timed out, or no containers are running)"
# --- 3. Gerrit->GitHub replication failures (S5) ---------------------------
# Watch the replication plugin's log for failure signatures and publish the COUNT
# of NEW failure lines since last run to rebar/host:replication_errors (the metric
# the S5 CloudWatch alarm watches). A persisted line-count offset turns the
# cumulative grep into a per-interval delta. Failure signatures: a hard-rejected
# non-fast-forward push (the one-way-door violation), max-retry exhaustion, ERROR.
REPL_LOG="${REPL_LOG:-/var/gerrit/site/logs/replication_log}"
REPL_OFFSET_FILE="${REPL_OFFSET_FILE:-/var/lib/rebar/repl-fail-offset}"
if [ -f "$REPL_LOG" ]; then
  mkdir -p "$(dirname "$REPL_OFFSET_FILE")"
  # NOTE: `grep -c` prints 0 AND exits 1 on zero matches; do NOT add `|| echo 0`
  # (that would append a SECOND "0" line and corrupt the arithmetic). Capture the
  # single-line count and default-empty-to-0 instead.
  # NOT record-anchored, unlike the marker counters in §4/§4c/§4d (bug 8c2f-8377-5044-4650): this
  # reads Gerrit's replication_log, which no LLM writes to, and the signatures are free-form
  # phrases inside log lines rather than line-start records — an anchor would drop real failures.
  total=$(grep -cE 'REJECTED_NONFASTFORWARD|non-fast-forward|Giving up|giving up after|\[ERROR\]' "$REPL_LOG" 2>/dev/null) || true
  total=${total:-0}
  prev=$(cat "$REPL_OFFSET_FILE" 2>/dev/null || true)
  # NO OFFSET YET = COLD START -> SEED, NEVER REPUBLISH THE JOURNAL (bug e2a6-9ee4-8d5c-4290).
  # An absent offset file means this counter has NEVER observed the source (a newly introduced
  # counter, a fresh /var/lib/rebar, a host rebuild, a disk restore), so every marker already in
  # the journal predates monitoring by it. Defaulting prev to 0 made the first run publish the
  # ENTIRE retained journal as one interval delta — on 2026-08-12 that fabricated 7
  # bound-exceeded + 1 signal-unavailable markers from history reaching back to 2026-08-04,
  # against 1-datapoint threshold>0 alarms. Seeding prev to $total publishes 0 for the
  # initialising run and the publish-then-persist block below writes $total to the offset, so the
  # counter measures from the next interval on. This is the cold-start complement of the
  # negative-delta clamp above: there lost history is already-counted, here inherited history is
  # never-monitored. An empty or non-numeric offset takes the same branch — an unreadable offset
  # is indistinguishable from none, and republishing on it fabricates identically. A file holding
  # a real "0" still parses as 0 and is unaffected.
  case "$prev" in ''|*[!0-9]*) prev=$total ;; esac
  new=$(( total - prev ))
  # NEGATIVE DELTA = LOST HISTORY -> SUPPRESS, NEVER REPUBLISH (bug 2dc7-31b7-ecbb-4cd2).
  # total < prev only when the source LOST entries it previously had (journald vacuuming by
  # SystemMaxUse/MaxRetentionSec, `journalctl --vacuum-*`, a truncated/rotated log). The
  # offset is monotonic evidence that those entries were already published, so every
  # surviving entry is at-or-older-than something already counted: republishing $total would
  # be guaranteed DOUBLE-counting, and on a 1-datapoint alarm (review_interrupts) that is a
  # false page whose evidence has just been deleted. Publishing 0 instead under-counts at
  # most the markers emitted inside the single rotating interval, and self-heals immediately:
  # the offset is rewritten to the new smaller $total below, so genuinely-new markers after
  # the rotation publish normally on the next run.
  [ "$new" -lt 0 ] && new=0
  # Published WITHOUT dimensions to match the dimensionless alarm in monitoring_s5.tf
  # (CloudWatch keys a metric by namespace+name+dimensions; the alarm has none).
  if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name replication_errors --unit Count --value "$new" 2>/dev/null; then
    echo "$total" > "$REPL_OFFSET_FILE"
  fi
  [ "$new" -gt 0 ] && logger -t rebar-health "replication failures (new this interval)=${new}"
else
  # NO LOG = STILL A HEARTBEAT (ticket bff5-9163-cddd-4158). monitoring_s5.tf treats missing
  # replication_errors data as BREACHING on the ground that this section publishes every
  # interval, so the section has to actually do that. The log is absent on a rebuilt host, on a
  # site volume that has not mounted yet, and before Gerrit's replication plugin has ever
  # written — none of which is a replication FAILURE, and all of which would otherwise page
  # this alarm continuously with no failure to point at.
  # Publishing 0 does NOT hide "replication stopped": that outcome is GitHub `main` falling
  # behind Gerrit `main`, which is exactly what §5's mirror_out_of_sync (monitoring_ws7.tf)
  # measures directly, and dead-publisher detection is unaffected because the datapoint is
  # still emitted every run. The journald line below is the diagnostic for the absence itself.
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name replication_errors --unit Count --value 0 2>/dev/null || true
  logger -t rebar-health "replication log ${REPL_LOG} absent; published replication_errors=0 heartbeat"
fi

# --- 4. review-bot LLM-Review voter failures (S4b) -------------------------
# Watch the review-bot container's journald for the structured VOTER_ERROR marker
# the voter emits when it cannot cast a vote (Gerrit 4xx/5xx, clone/diff failure,
# LLM unavailable, expired token) and publish the COUNT of NEW markers since last
# run to rebar/host:voter_errors (the metric the S4b CloudWatch alarm watches).
# Same shape as the replication_errors section above: a persisted cumulative count
# turned into a per-interval delta via an offset file. The voter writes VOTER_ERROR
# to stderr, which compose's journald driver ships under CONTAINER_NAME=compose-review-bot-1.
# Greping journald on the HOST avoids giving the container AWS creds (the IMDS hop
# limit constrains in-container metadata access).
VOTER_CONTAINER="${VOTER_CONTAINER:-compose-review-bot-1}"
VOTER_OFFSET_FILE="${VOTER_OFFSET_FILE:-/var/lib/rebar/voter-fail-offset}"
mkdir -p "$(dirname "$VOTER_OFFSET_FILE")"
# NOTE: `grep -c` prints 0 AND exits 1 on zero matches; do NOT add `|| echo 0`
# (that would append a SECOND "0" line and corrupt the arithmetic). Capture the
# single-line count and default-empty-to-0 instead.
# ANCHOR TO THE EMITTED RECORD, NOT THE BARE TOKEN (bug 8c2f-8377-5044-4650).
# The review-bot writes its LLM review output to the SAME journal stream it emits markers on, so an
# unanchored 'VOTER_ERROR' counted any review whose text merely NAMED the marker. On 2026-08-12 a
# review of this very file enumerated the marker vocabulary in prose and fired both the voter and
# merge-change alarms; the merge-change counter had never seen a real error in the whole retained
# journal. Real markers are emitted as `<TOKEN> {json}` at the start of the message (voter.py
# prints "VOTER_ERROR " + json.dumps(record) to stderr; autodeploy.sh marker() does the same
# shape), and journalctl -o cat prints the message alone — so `^<TOKEN> \{` matches every genuine
# record (evidence: 221 line-start == 221 real) and no prose, whether the token appears mid-line or
# opens one. A token is an alarm contract: keep this pattern in step with the emitter.
# Since bug f829-152a-b415-44a4 the emitters' logger-stream copy logs the JSON record body
# WITHOUT the line-start token — only the stderr print emits `<TOKEN> {json}` — so configured
# application logging (whose stdout also lands in journald) cannot double this count.
vtotal=$(journalctl CONTAINER_NAME="$VOTER_CONTAINER" --no-pager -o cat 2>/dev/null | grep -cE '^VOTER_ERROR \{') || true
vtotal=${vtotal:-0}
vprev=$(cat "$VOTER_OFFSET_FILE" 2>/dev/null || true)
# No offset yet -> seed to $total and publish 0, never the inherited journal; rationale at the
# replication_errors counter (§3).
case "$vprev" in '' | *[!0-9]*) vprev=$vtotal ;; esac
vnew=$((vtotal - vprev))
# Lost history -> publish 0, never $total; rationale at the replication_errors counter (§3).
[ "$vnew" -lt 0 ] && vnew=0
# Published WITHOUT dimensions to match the dimensionless alarm in monitoring_s4b.tf
# (CloudWatch keys a metric by namespace+name+dimensions; the alarm has none).
if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name voter_errors --unit Count --value "$vnew" 2>/dev/null; then
  echo "$vtotal" >"$VOTER_OFFSET_FILE"
fi
[ "$vnew" -gt 0 ] && logger -t rebar-health "review-bot voter failures (new this interval)=${vnew}"

# --- 4c. review-bot merge-change path failures (epic 88ab / S2) -------------
# The merge-change review path (a merge revision reviewed on its auto-merge delta only)
# writes a structured MERGE_CHANGE_ERROR marker to stderr when a merge-path REST call
# (files / mergelist / per-file diff) fails. This is a GRANULAR diagnosis metric — those
# same failures ALSO surface in voter_errors above (the voter fails closed), but this
# metric isolates "the merge path specifically is broken" from general voter failure.
# Same offset-delta shape as section 4; published WITHOUT dimensions to match the
# dimensionless alarm in monitoring_88ab.tf.
MERGE_OFFSET_FILE="${MERGE_OFFSET_FILE:-/var/lib/rebar/merge-change-fail-offset}"
mkdir -p "$(dirname "$MERGE_OFFSET_FILE")"
# Record-anchored like the voter counter above; rationale at section 4 (bug 8c2f-8377-5044-4650).
mtotal=$(journalctl CONTAINER_NAME="$VOTER_CONTAINER" --no-pager -o cat 2>/dev/null | grep -cE '^MERGE_CHANGE_ERROR \{') || true
mtotal=${mtotal:-0}
mprev=$(cat "$MERGE_OFFSET_FILE" 2>/dev/null || true)
# No offset yet -> seed to $total and publish 0, never the inherited journal; rationale at the
# replication_errors counter (§3).
case "$mprev" in '' | *[!0-9]*) mprev=$mtotal ;; esac
mnew=$((mtotal - mprev))
# Lost history -> publish 0, never $total; rationale at the replication_errors counter (§3).
[ "$mnew" -lt 0 ] && mnew=0
if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name review_bot_merge_change_errors --unit Count --value "$mnew" 2>/dev/null; then
  echo "$mtotal" >"$MERGE_OFFSET_FILE"
fi
[ "$mnew" -gt 0 ] && logger -t rebar-health "review-bot merge-change failures (new this interval)=${mnew}"

# --- 4d. continuous auto-deploy failures (epic 88ab / story 8903) -----------
# autodeploy.sh (the systemd oneshot rebar-autodeploy.service) writes an AUTODEPLOY_ERROR
# marker to stderr -> journald whenever a deploy step fails (fetch, config-check, build,
# health-check-then-rollback, etc.). It is a systemd UNIT (not a container), so grep its
# unit journal (not a CONTAINER_NAME). Same offset-delta shape as above; published without
# dimensions to match the dimensionless alarm in monitoring_autodeploy.tf. A persistent
# signal here means the box is NOT tracking main (drifting) and/or a deploy is failing +
# backing off — the last-known-good stays live, but an operator should investigate.
DEPLOY_OFFSET_FILE="${DEPLOY_OFFSET_FILE:-/var/lib/rebar/autodeploy-fail-offset}"
mkdir -p "$(dirname "$DEPLOY_OFFSET_FILE")"
# Record-anchored like the voter counter (§4). This unit's journal is not LLM-written, but it
# DOES echo captured review-bot output (autodeploy.sh capture_bot_logs), which is why that function
# redacts the token — the anchor makes the counter robust regardless (bug 8c2f-8377-5044-4650).
dtotal=$(journalctl -u rebar-autodeploy.service --no-pager -o cat 2>/dev/null | grep -cE '^AUTODEPLOY_ERROR \{') || true
dtotal=${dtotal:-0}
dprev=$(cat "$DEPLOY_OFFSET_FILE" 2>/dev/null || true)
# No offset yet -> seed to $total and publish 0, never the inherited journal; rationale at the
# replication_errors counter (§3).
case "$dprev" in '' | *[!0-9]*) dprev=$dtotal ;; esac
dnew=$((dtotal - dprev))
# Lost history -> publish 0, never $total; rationale at the replication_errors counter (§3).
[ "$dnew" -lt 0 ] && dnew=0
if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name deploy_errors --unit Count --value "$dnew" 2>/dev/null; then
  echo "$dtotal" >"$DEPLOY_OFFSET_FILE"
fi
[ "$dnew" -gt 0 ] && logger -t rebar-health "auto-deploy failures (new this interval)=${dnew}"


# --- 4e. review-drain outcomes: deferrals + interrupted reviews (bug 34cd) --
# autodeploy.sh will not recreate the review-bot container while a review is in flight —
# recreating it KILLS the review, and does so INVISIBLY: nothing fails, so the voter emits no
# VOTER_ERROR (§4 above stays 0), `restarts` stays 0, and the deploy still logs "redeployed +
# healthy". That is why a fully live-locked LLM-Review gate could sit behind eleven green
# alarms. These two counters are what make the failure mode visible at all:
#   deploy_deferrals   — deploys skipped to protect a review. NOT an error: expected during a
#                        landing burst, and each one is bounded (DEPLOY_DEFER_MAX). A sustained
#                        signal means `main` is reaching the box more slowly than usual.
#   review_interrupts  — a review WAS (or may have been) killed: the deferral bound was
#                        exhausted, or the in-flight signal itself was unreadable so the deploy
#                        ran blind. This is the alarm-worthy one.
# Counted from the UNIT journal with the same offset-delta shape as §4d, and published
# dimensionless to match the alarms. Kept as distinct tokens from AUTODEPLOY_ERROR on purpose:
# folding a routine, healthy deferral into deploy_errors would page on normal burst behaviour.
#
# SPLIT BY REASON (bug 613a). The interrupt marker carries a `reason` the rolled-up counter used
# to discard, and the two reasons have OPPOSITE remediations:
#   bound-exceeded      — DEPLOY_DEFER_MAX was spent with reviews still in flight. The bot is
#                         chronically busy; the drain check itself is WORKING.
#   signal-unavailable  — /health's in_flight was unreadable, so the deploy ran with NO drain
#                         check. The probe is broken and every deploy is blind. The urgent one.
# A CloudWatch-only sweep could not tell them apart, so answering [rebar:7b4a-0f39-1a45-4ce9]
# needed SSM shell access to read the journal. Each reason is now counted into its OWN metric,
# so the alarm carries the remediation. Done with distinct journal PATTERNS into distinct metric
# names — not a CloudWatch dimension — mirroring how deploy_deferrals is already kept separate,
# and keeping every side of this dimensionless (a dimension added on only one side silently
# unmatches; see monitoring_s4b.tf). The rolled-up `review_interrupts` is still published so
# pre-split history stays readable and any future third reason is still counted somewhere; only
# the per-reason metrics are alarmed, so one interrupt never double-pages.
DEFER_OFFSET_FILE="${DEFER_OFFSET_FILE:-/var/lib/rebar/autodeploy-defer-offset}"
INTERRUPT_OFFSET_FILE="${INTERRUPT_OFFSET_FILE:-/var/lib/rebar/autodeploy-interrupt-offset}"
INTERRUPT_BOUND_OFFSET_FILE="${INTERRUPT_BOUND_OFFSET_FILE:-/var/lib/rebar/autodeploy-interrupt-bound-offset}"
INTERRUPT_SIGNAL_OFFSET_FILE="${INTERRUPT_SIGNAL_OFFSET_FILE:-/var/lib/rebar/autodeploy-interrupt-signal-offset}"
DISK_PRESSURE_OFFSET_FILE="${DISK_PRESSURE_OFFSET_FILE:-/var/lib/rebar/autodeploy-disk-pressure-offset}"
DISK_PRESSURE_PERSIST_OFFSET_FILE="${DISK_PRESSURE_PERSIST_OFFSET_FILE:-/var/lib/rebar/autodeploy-disk-pressure-persist-offset}"
# mcp blue-green target (panicky-sylphish-foxterrier). Two DISTINCT tokens, kept out of
# AUTODEPLOY_ERROR so a routine retire-cap / memory abort never inflates deploy_errors:
#   mcp_retire_cap — the blue/green port pool is exhausted (both A and B held by un-reaped
#                    containers still draining); the deploy backed off rather than force-killing a
#                    live container. Sustained = mcp releases are not draining / a stuck container.
#   mcp_mem_abort  — the 8 GiB box was below the memory floor, so the blue-green 2x overlap was
#                    refused before the second container started. Sustained = the box is memory-bound.
MCP_RETIRE_CAP_OFFSET_FILE="${MCP_RETIRE_CAP_OFFSET_FILE:-/var/lib/rebar/autodeploy-mcp-retire-cap-offset}"
MCP_MEM_ABORT_OFFSET_FILE="${MCP_MEM_ABORT_OFFSET_FILE:-/var/lib/rebar/autodeploy-mcp-mem-abort-offset}"
mkdir -p "$(dirname "$DEFER_OFFSET_FILE")" "$(dirname "$INTERRUPT_OFFSET_FILE")" \
  "$(dirname "$INTERRUPT_BOUND_OFFSET_FILE")" "$(dirname "$INTERRUPT_SIGNAL_OFFSET_FILE")" \
  "$(dirname "$DISK_PRESSURE_OFFSET_FILE")" "$(dirname "$DISK_PRESSURE_PERSIST_OFFSET_FILE")" \
  "$(dirname "$MCP_RETIRE_CAP_OFFSET_FILE")" "$(dirname "$MCP_MEM_ABORT_OFFSET_FILE")"
publish_autodeploy_marker_delta() {
  local token="$1" metric="$2" offset_file="$3" label="$4" total prev new
  total=$(journalctl -u rebar-autodeploy.service --no-pager -o cat 2>/dev/null | grep -cE "$token") || true
  total=${total:-0}
  prev=$(cat "$offset_file" 2>/dev/null || true)
  # No offset yet -> seed to $total and publish 0, never the inherited journal; rationale at
  # the replication_errors counter (§3).
  case "$prev" in '' | *[!0-9]*) prev=$total ;; esac
  new=$((total - prev))
  # Lost history -> publish 0, never $total; rationale at the replication_errors counter (§3).
  [ "$new" -lt 0 ] && new=0
  if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name "$metric" --unit Count --value "$new" 2>/dev/null; then
    echo "$total" >"$offset_file"
  fi
  [ "$new" -gt 0 ] && logger -t rebar-health "${label} (new this interval)=${new}"
  return 0
}
# The patterns are record-anchored (`^<TOKEN> \{`) so prose naming a marker is never counted;
# rationale at section 4 (bug 8c2f-8377-5044-4650). The anchor lives at the CALL SITES because the
# helper's first argument is an ERE the reason-scoped counters extend.
publish_autodeploy_marker_delta '^AUTODEPLOY_DEFERRED \{' deploy_deferrals \
  "$DEFER_OFFSET_FILE" "auto-deploys deferred for an in-flight review"
publish_autodeploy_marker_delta '^AUTODEPLOY_REVIEW_INTERRUPT \{' review_interrupts \
  "$INTERRUPT_OFFSET_FILE" "review-bot reviews interrupted by a deploy"
# The first argument is an ERE handed to `grep -cE`, so the reason-scoped counters select on the
# marker's JSON payload (autodeploy.sh `marker()` emits
# `AUTODEPLOY_REVIEW_INTERRUPT {"ts": …, "reason": "<reason>", "detail": …}`). The `[[:space:]]*`
# keeps the match independent of the JSON separator spacing.
publish_autodeploy_marker_delta \
  '^AUTODEPLOY_REVIEW_INTERRUPT \{.*"reason":[[:space:]]*"bound-exceeded"' \
  review_interrupts_bound_exceeded "$INTERRUPT_BOUND_OFFSET_FILE" \
  "reviews interrupted after the deferral bound was exhausted (review-bot chronically busy)"
publish_autodeploy_marker_delta \
  '^AUTODEPLOY_REVIEW_INTERRUPT \{.*"reason":[[:space:]]*"signal-unavailable"' \
  review_interrupts_signal_unavailable "$INTERRUPT_SIGNAL_OFFSET_FILE" \
  "reviews interrupted with the in-flight signal UNREADABLE (deploys are running blind)"
# Pressure-triggered reclaims on the quiescent no-op tick (autodeploy.sh reclaim_under_pressure,
# story 28f9). A diagnostic counter, not an alarm input — the OUTCOME is already alarmed by
# rebar-root-disk-pressure. It exists so an incident sweep can distinguish "the reclaim gate
# never ran" from "it ran and reclaimed nothing" without host access (task 9d15-d576-e0ca-4596).
publish_autodeploy_marker_delta '^AUTODEPLOY_DISK_PRESSURE \{' disk_pressure_prunes \
  "$DISK_PRESSURE_OFFSET_FILE" "auto-deploy disk-pressure prunes"
# …and the counter that closes the gap the comment above CLAIMED to close but could not
# (bug 9bc0). disk_pressure_prunes counts INVOCATIONS, so "the gate never ran" and "it ran and
# reclaimed nothing" are the same number. AUTODEPLOY_DISK_PRESSURE_PERSISTS is emitted only
# after PRESSURE_STREAK_ALARM (default 3) CONSECUTIVE reclaim cycles each completed with the
# disk still pressured, so it fires on PERSISTENCE, not on pressure — the discriminator the
# flapping rebar-root-disk-pressure threshold alarm cannot express. A single pressured cycle
# that then recovers resets the streak and publishes nothing here.
#
# The token is DISTINCT from AUTODEPLOY_DISK_PRESSURE and the patterns cannot cross-count: both
# are record-anchored and require the JSON `{` immediately after the token, so
# `^AUTODEPLOY_DISK_PRESSURE \{` never matches `AUTODEPLOY_DISK_PRESSURE_PERSISTS {`.
publish_autodeploy_marker_delta '^AUTODEPLOY_DISK_PRESSURE_PERSISTS \{' disk_pressure_persists \
  "$DISK_PRESSURE_PERSIST_OFFSET_FILE" \
  "reclaim cycles that ran and left the disk STILL pressured (reclaim is ineffective)"
# mcp blue-green target (panicky-sylphish-foxterrier). Record-anchored like every counter above;
# each kept out of deploy_errors so a routine retire-cap / memory abort never pages that alarm.
publish_autodeploy_marker_delta '^AUTODEPLOY_MCP_RETIRE_CAP \{' mcp_retire_cap \
  "$MCP_RETIRE_CAP_OFFSET_FILE" "mcp blue-green retire/port-pool cap hits (releases not draining)"
publish_autodeploy_marker_delta '^AUTODEPLOY_MCP_MEM_ABORT \{' mcp_mem_abort \
  "$MCP_MEM_ABORT_OFFSET_FILE" "mcp blue-green deploys aborted for low memory on the 8GiB box"


# --- 4b. gerrit-to-platform CI-dispatch failures (epic 1fa8) ---------------
# Watch the GERRIT container's journald for gerrit-to-platform (g2p) error markers
# and publish the COUNT of NEW markers since last run to rebar/host:g2p_dispatch_errors
# (the metric the epic-1fa8 CloudWatch alarm in monitoring_1fa8.tf watches).
#
#   LOG SOURCE:   the Gerrit container's journald — CONTAINER_NAME=compose-gerrit-1.
#                 The `hooks` plugin execs the in-container g2p console-scripts on
#                 patchset-created / `recheck`; their stdout/stderr ships here (the
#                 compose journald driver, docker-compose.yml). This is the DISPATCH
#                 leg (Gerrit -> GitHub workflow_dispatch); the vote-back leg lives in
#                 the GitHub Actions run status, not on this host (see ADR-0023).
#   GREP PATTERN: g2p logs under the `gerrit_to_platform` logger; a dispatch failure
#                 shows as that token with an error level / traceback, or an explicit
#                 workflow_dispatch failure, or a GitHub 4xx/5xx from the dispatch call.
#                 Case-insensitive (-iE) so casing drift in g2p's messages still matches;
#                 tune the phrases here if g2p's actual log strings differ in prod.
#   METRIC NAME:  rebar/host:g2p_dispatch_errors (DIMENSIONLESS, like the sections above).
#
# Same shape as sections 3/4: a persisted cumulative count turned into a per-interval
# delta via an offset file. Greping journald on the HOST avoids giving the container
# AWS creds (the IMDS hop limit constrains in-container metadata access).
G2P_CONTAINER="${G2P_CONTAINER:-compose-gerrit-1}"
G2P_OFFSET_FILE="${G2P_OFFSET_FILE:-/var/lib/rebar/g2p-fail-offset}"
G2P_PATTERN="${G2P_PATTERN:-gerrit_to_platform.*(error|critical|traceback|exception)|failed to dispatch|workflow_dispatch.*(fail|error)|dispatch.*http (4|5)[0-9][0-9]}"
mkdir -p "$(dirname "$G2P_OFFSET_FILE")"
# NOTE: `grep -c` prints 0 AND exits 1 on zero matches; do NOT add `|| echo 0`
# (that would append a SECOND "0" line and corrupt the arithmetic). Capture the
# single-line count and default-empty-to-0 instead.
# NOT record-anchored (see §4, bug 8c2f-8377-5044-4650): the Gerrit container's journal is not
# LLM-written and these are free-form phrase/level matches, not line-start records.
gtotal=$(journalctl CONTAINER_NAME="$G2P_CONTAINER" --no-pager -o cat 2>/dev/null | grep -ciE "$G2P_PATTERN") || true
gtotal=${gtotal:-0}
gprev=$(cat "$G2P_OFFSET_FILE" 2>/dev/null || true)
# No offset yet -> seed to $total and publish 0, never the inherited journal; rationale at the
# replication_errors counter (§3).
case "$gprev" in '' | *[!0-9]*) gprev=$gtotal ;; esac
gnew=$((gtotal - gprev))
# Lost history -> publish 0, never $total; rationale at the replication_errors counter (§3).
[ "$gnew" -lt 0 ] && gnew=0
# Published WITHOUT dimensions to match the dimensionless alarm in monitoring_1fa8.tf
# (CloudWatch keys a metric by namespace+name+dimensions; the alarm has none).
if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name g2p_dispatch_errors --unit Count --value "$gnew" 2>/dev/null; then
  echo "$gtotal" >"$G2P_OFFSET_FILE"
fi
[ "$gnew" -gt 0 ] && logger -t rebar-health "g2p CI-dispatch failures (new this interval)=${gnew}"

# --- 5. Gerrit->GitHub mirror out-of-sync (WS7 / a774) ---------------------
# After the mirror-lock cutover, GitHub `main` only advances via Gerrit replication.
# If replication is stuck/failing, GitHub `main` falls BEHIND Gerrit `main` while Gerrit
# keeps moving — a silent drift the S5 error-count probe (section 3) does NOT catch (a
# push that never fires logs no failure). Publish mirror_out_of_sync = 1 when the two
# `main` SHAs differ, else 0. Both reads are ANONYMOUS (Gerrit public REST + a public
# `git ls-remote`), so no credentials are needed on the box. Transient lag (~15s after a
# submit) is absorbed by the alarm's multi-period evaluation window (monitoring_ws7.tf),
# not here.
#
# ON A FETCH FAILURE WE PUBLISH 1 — the alarm's breaching value (ticket bff5-9163-cddd-4158).
# This section used to publish NOTHING there, which was a FAIL-OPEN: a live, healthy host whose
# Gerrit REST read or `git ls-remote` breaks would mute the alarm indefinitely while GitHub
# `main` silently drifted, and the alarm's own treat_missing_data read that silence as health.
# Publishing 0 would not fix it either — 0 means "in sync", which is precisely the claim a
# failed comparison cannot make. So an unmakeable comparison reports the unsafe value, and the
# alarm's 2-of-3 five-minute window (monitoring_ws7.tf) absorbs an isolated fetch blip: it takes
# two breaching datapoints inside 15 minutes to page, which a one-off curl timeout cannot reach.
GERRIT_BASE_URL="${GERRIT_BASE_URL:-https://rebar.solutions.navateam.com}"
GITHUB_REPO_URL="${GITHUB_REPO_URL:-https://github.com/navapbc/rebar}"
gerrit_sha=$(curl -fsS --max-time 10 "${GERRIT_BASE_URL}/projects/rebar/branches/main" 2>/dev/null \
  | sed "s/)]}'//" | grep -oE '"revision": ?"[0-9a-f]+"' | grep -oE '[0-9a-f]{40}')
github_sha=$(git ls-remote "${GITHUB_REPO_URL}" refs/heads/main 2>/dev/null | awk '{print $1}')
if [ -n "$gerrit_sha" ] && [ -n "$github_sha" ]; then
  if [ "$gerrit_sha" = "$github_sha" ]; then oos=0; else oos=1; fi
else
  # Could not compare. Report the breaching value rather than staying silent (see above).
  oos=1
  logger -t rebar-health "mirror sync check failed, publishing mirror_out_of_sync=1 (gerrit='${gerrit_sha}' github='${github_sha}')"
fi
# Dimensionless to match the alarm in monitoring_ws7.tf. Published on EVERY run, including the
# failed-comparison path, so the metric is continuously present and its absence means the probe
# itself is dead — which is what the alarm's treat_missing_data = "breaching" now catches.
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name mirror_out_of_sync --unit Count --value "$oos" 2>/dev/null || true
[ "$oos" -gt 0 ] && logger -t rebar-health "mirror out-of-sync: gerrit=${gerrit_sha} github=${github_sha}"

# Always exit success on a completed probe run. Without this, the script's exit
# status is that of its last statement — and every metric section ends in a
# `[ "$n" -gt 0 ] && logger …` guard that is *false* on a healthy box (n=0),
# making the whole probe exit 1 and marking the systemd oneshot `failed` (which
# trips the deploy/health alarms). The probe reports state via metrics/journald,
# not its exit code; a run that reached here completed successfully.
exit 0
