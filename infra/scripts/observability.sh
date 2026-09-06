#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# observability.sh — the rebar box's host observability probe (S2 + S5 + S4b + S7 + 1fa8).
#
# Run periodically by a systemd timer (install-observability.sh). Each run publishes
# CloudWatch metrics + journald log lines:
#   0. The probe's OWN liveness -> rebar/host:{probe_ok,probe_elapsed_seconds,probe_truncated}
#      (bug 9313-1fac-9f32-4b07; two alarms in monitoring_9313.tf). Everything else here
#      watches something this probe measures; nothing watched the probe, so when it was being
#      SIGTERM-ed on its 240s TimeoutStartSec every run under load, the metrics published after
#      the kill point simply vanished and read as "sparse publishing" for a day. probe_ok is
#      emitted once, LAST, so its absence means the run did not reach the end; probe_truncated
#      is emitted by the ExecStopPost hook, which systemd runs even after that SIGTERM.
#   1. Health probe of Gerrit + the review-bot (/review/health) -> journald +
#      rebar/host:{gerrit_healthy,reviewbot_healthy} (S2).
#   1b. Health probe of the rebar MCP SERVING PATH (https://<domain>/mcp through nginx and
#      the materialized `upstream rebar_mcp` include) -> rebar/host:mcp_healthy, a 1/0
#      heartbeat published on every tick (bug 9ea3-7d07-ea55-4496; alarm in monitoring_9ea3.tf).
#   2. Gerrit data-volume disk-used-percent -> rebar/host:disk_used_percent (S2 alarm).
#   2h. /var/tmp, the fourth root generator -> rebar/host:var_tmp_bytes,
#       var_tmp_used_percent, var_tmp_cleanup_active and var_tmp_hard_quota_in_effect
#       (ADR 0112, story 2ba3; alarms in monitoring_autodeploy.tf). The last one is the
#       point: /var/tmp has no native writer-enforced cap, so the box publishes whether it
#       is holding a real XFS quota ceiling or only a timer-driven mitigation.
#   2i. Writable CONTAINER layers, the last root generator and the part of §2f no image or
#       build-cache prune can reach -> rebar/host:container_writable_bytes,
#       container_exited_bytes, container_writable_used_percent, container_reaper_active and
#       container_quota_enforceable (ADR 0112, story 910b; two alarms in
#       monitoring_autodeploy.tf). The last two are the point: the share is held by a reaper that
#       can only remove EXITED containers, and the one true per-container ceiling (overlay2
#       --storage-opt size=) needs XFS pquota and a reboot — so the box publishes what is holding
#       the line instead of a runbook asserting it.
#   2c. Non-`site/` debris on the Gerrit data volume (bytes under /var/gerrit that are not
#       the Gerrit site tree) -> rebar/host:data_disk_debris_bytes (task 3e92 alarm). Answers
#       "full OF WHAT", which the used-percent reading in 2 structurally cannot.
#   2d. Host memory (mem_available_percent / mem_used_percent / mem_probe_ok) and
#       per-container resident set (container_memory_rss_bytes, `container` dimension, plus
#       the container_stats_ok census heartbeat) -> rebar/host (bug 9ea3; measurement only,
#       no alarm yet).
#   2f. Docker storage generators -> rebar/host:docker_storage_bytes,
#       docker_storage_used_percent, docker_buildkit_cache_bytes,
#       docker_buildkit_cache_used_percent and docker_unaccounted_bytes (ADR 0112 / story
#       9183; three alarms in monitoring_autodeploy.tf). The last one is the point: it is
#       overlay2 measured from the FILESYSTEM minus what `docker system df` accounts for, i.e.
#       the bytes no `docker prune` can reach.
#   2g. journald, the other /var/log generator -> rebar/host:journal_bytes,
#       journal_used_percent and the journal_cap_in_effect heartbeat (ADR 0112 / story e956;
#       two alarms in monitoring_autodeploy.tf). The heartbeat is the point: journald reads its
#       ceiling only at startup, so a drop-in installed under a live daemon is dormant — and
#       the percentage is then measured against a cap that is NOT in force.
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

# --- WHOLE-PROBE DEADLINE (bug 9313-1fac-9f32-4b07) -------------------------
# Every expensive call in this probe was bounded INDEPENDENTLY, and nothing composed those
# bounds. The journal section reasons "12 scans x 10 s = 120 s < the 240 s TimeoutStartSec"
# (§ BOUNDED JOURNAL COUNTING) while the docker section reasons about its own 120 s ceiling —
# and the sum of the two arguments is larger than the budget both are spending. On
# the production Gerrit host the docker walk alone reached that 240 s and systemd SIGTERM-ed the run,
# so every section AFTER it published nothing at all: 55 timeout kills against 197 completed
# runs in 24 h, clustered under I/O load, i.e. exactly when an operator reads these metrics.
#
# So the budget is now held in ONE place. PROBE_TAIL_RESERVE_SEC is carved out for the cheap
# sections that come last (memory, the container census, the marker counters, the mirror
# check), and `clamped` hands every bounded call the smaller of its own ceiling and what is
# left after that reserve. An overrunning section can then degrade its OWN reading — reported
# as silence, the §2e rule — but can no longer starve a later one. That is the answer to
# "metric publication must not depend on position in the script": not a re-ordering, which only
# moves which metric starves, but a reserve no earlier section can spend.
#
# THE INVARIANT THIS BUYS. `clamped` grants at most (PROBE_DEADLINE_SEC - elapsed -
# PROBE_TAIL_RESERVE_SEC), so a call starting at t cannot end after PROBE_DEADLINE_SEC -
# PROBE_TAIL_RESERVE_SEC no matter what it is, and no sequence of them can either. The clamped
# portion of a run is therefore bounded at 240 - 80 = 160 s BY CONSTRUCTION rather than by
# estimate, leaving 80 s of headroom under TimeoutStartSec for the unclamped arithmetic and the
# `aws put-metric-data` calls. Before this, the same worst case summed to over 495 s — two
# 120 s docker walks, a 20 s ledger read, two 60 s `du`s over /var/log/journal and /var/tmp, a
# per-entry `du` loop, two 15 s docker calls, seven 10 s journal reads and a 15 s `git
# ls-remote` — inside a 240 s timeout, with each ceiling defensible on its own and no one
# adding them up.
#
# Measured tail cost on the production Gerrit host (2026-09-05, load average 3.45): docker ps 0.03 s,
# docker stats 3.22 s, `free` 0.02 s, three health curls 0.69 s, git ls-remote 0.25 s, and the
# seven cursor-anchored journal reads 0.49-1.69 s each — ~15 s in total against the 80 s
# reserved for it.
#
# THE INVARIANT IS BUILD-ENFORCED, not a convention. `clamped` composes ceilings only while it
# is the ONLY door, and the original defect accumulated exactly one defensible ceiling at a
# time — so a bare `timeout` added here next month would reintroduce it with no symptom until
# the unit starts being killed again. tests/scripts/test_observability_timeout_9313.py's
# `test_every_wall_clock_bound_goes_through_clamped` is a source scan that FAILS if either
# helper appears in command position outside the two lines that form the door itself. Add a new
# bounded call through `clamped`, or that test will tell you why you must.
#
# mechanism-ok: env_var PROBE_DEADLINE_SEC — 9313-1fac-9f32-4b07: the whole-probe wall-clock
# budget, which must track TimeoutStartSec in install-observability.sh; overridable so the
# tests can drive the exhausted-budget path without waiting four minutes.
PROBE_DEADLINE_SEC="${PROBE_DEADLINE_SEC:-240}"
# mechanism-ok: env_var PROBE_TAIL_RESERVE_SEC — 9313-1fac-9f32-4b07: the share of that budget
# reserved for the sections after the expensive ones, so none can be starved by an overrun.
PROBE_TAIL_RESERVE_SEC="${PROBE_TAIL_RESERVE_SEC:-80}"
PROBE_STARTED_AT="$(date +%s)"

# Seconds an expensive call may still spend without eating the tail reserve. Floored at 1
# rather than 0: a clamped call must still RUN and fail fast, because "could not be measured"
# is a publishable state and being SIGKILLed is not.
probe_budget_left() {
  local left
  left=$(( PROBE_DEADLINE_SEC - ( $(date +%s) - PROBE_STARTED_AT ) - PROBE_TAIL_RESERVE_SEC ))
  [ "$left" -lt 1 ] && left=1
  printf '%s\n' "$left"
}

# --- TRUNCATION IS PUBLISHED, NOT INFERRED (bug 9313-1fac-9f32-4b07) --------
# `--report-exit` is the ExecStopPost hook installed by install-observability.sh. systemd runs
# ExecStopPost after the main process has gone, INCLUDING when it went because TimeoutStartSec
# SIGTERM-ed it — so a truncated run gets to publish its own death certificate.
#
# This is the property whose absence made the defect take a day to find. From the metric side,
# "the probe was killed part-way through" and "this section had nothing to report" were
# IDENTICAL: both are a gap, and every alarm here is treat_missing_data = "breaching", so both
# page for reasons an operator cannot tell apart. Of six alarms in ALARM at 17:09 UTC on
# 2026-09-05, four were firing on gaps while their underlying values were healthy —
# docker_unaccounted_bytes read 1.87 GB against a 2 GiB threshold.
#
# WHY THE GUARD IS HERE, AHEAD OF EVERYTHING. It was originally placed after the IMDS block, and
# that was wrong three times over. ExecStopPost runs on EVERY stop, so the successful path was
# re-executing the prologue for nothing. Worse, the hook then depended on the region curl
# answering — under I/O pressure, WHICH IS THE FAILURE THIS BUG IS ABOUT, that call can hang or
# fail, and `put-metric-data --region ''` publishes nothing. The truncation certificate would
# have been unable to report in precisely the condition it exists to report. And the stop path
# would have carried an unbudgeted 3 x 5 s of curls — the same uncomposed-ceiling defect this
# change exists to remove, reintroduced on the stop path.
#
# So the region is CACHED by the main run below and read back here. A truncation report needs no
# network at all in the normal case; the bounded single-shot IMDS call is only a cold-start
# fallback, and when even that yields nothing the hook logs to journald and exits rather than
# invoking `aws` with an empty --region. Reporting to journald and not to CloudWatch is a
# degraded report; inventing a region is a silent non-report.
#
# $SERVICE_RESULT is set by systemd for ExecStopPost ("success", "timeout", "signal", ...). It
# is empty when this is invoked by hand, which reports as untruncated rather than inventing a
# failure — the §2e rule, applied to the probe's own liveness.
# mechanism-ok: env_var REGION_CACHE — 9313-1fac-9f32-4b07: the region the truncation hook reads
# so it does not depend on IMDS answering during the stall it is reporting.
REGION_CACHE="${REGION_CACHE:-/var/lib/rebar/probe-region}"
# mechanism-ok: env_var DOCKER_DU_OVERLAY2_DEVCHECK_SKIP — 9313-1fac-9f32-4b07: lets the tests
# drive docker_du_census without a real filesystem behind $DOCKER_ROOT.
DOCKER_DU_OVERLAY2_DEVCHECK_SKIP="${DOCKER_DU_OVERLAY2_DEVCHECK_SKIP:-}"

if [ "${1:-}" = "--report-exit" ]; then
  probe_result="${SERVICE_RESULT:-success}"
  probe_truncated=1
  [ "$probe_result" = "success" ] && probe_truncated=0
  report_region="$(head -n 1 "$REGION_CACHE" 2>/dev/null || true)"
  case "$report_region" in *[!a-z0-9-]* | '') report_region="" ;; esac
  if [ -z "$report_region" ]; then
    # Cold start only: no run has cached a region yet. One bounded shot, then give up.
    report_token="$(curl -s --max-time 3 -X PUT http://169.254.169.254/latest/api/token \
      -H 'X-aws-ec2-metadata-token-ttl-seconds: 120' 2>/dev/null || true)"
    report_region="$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/placement/region \
      -H "X-aws-ec2-metadata-token: $report_token" 2>/dev/null || true)"
    case "$report_region" in *[!a-z0-9-]* | '') report_region="" ;; esac
  fi
  # journald FIRST and unconditionally: it needs no region, so the fact survives even when the
  # metric cannot be published.
  logger -t rebar-health \
    "probe exit report: SERVICE_RESULT=${probe_result} EXIT_CODE=${EXIT_CODE:-none} EXIT_STATUS=${EXIT_STATUS:-none} probe_truncated=${probe_truncated} region=${report_region:-unresolved}"
  if [ -n "$report_region" ]; then
    aws cloudwatch put-metric-data --region "$report_region" --namespace "$NS" \
      --metric-name probe_truncated --unit Count --value "$probe_truncated" 2>/dev/null || true
  fi
  exit 0
fi

# IMDSv2 region.
# BOUNDED, the §2d rule (bug 1205-63b2-2c01-4e7f). IMDS is link-local and normally answers in
# milliseconds, but these three run FIRST, before any metric is published, so a hang here takes
# the whole probe with it — and a `Type=oneshot` that never exits deletes its timer's next
# elapse rather than merely delaying it. The neighbouring health probes below were already
# `--max-time 10`; these were the outliers.
TOKEN=$(curl -s --max-time 5 -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 120')
REGION=$(curl -s --max-time 5 http://169.254.169.254/latest/meta-data/placement/region \
  -H "X-aws-ec2-metadata-token: $TOKEN")
IID=$(curl -s --max-time 5 http://169.254.169.254/latest/meta-data/instance-id \
  -H "X-aws-ec2-metadata-token: $TOKEN")

# Hand the region to the ExecStopPost hook above, which must not depend on IMDS answering during
# the stall it exists to report (bug 9313-1fac-9f32-4b07). Written only when it parses, so a bad
# read never overwrites a good cached value; best-effort, because a probe that cannot write here
# must still publish its metrics.
case "$REGION" in
  '' | *[!a-z0-9-]*) : ;;
  *)
    mkdir -p "$(dirname "$REGION_CACHE")" 2>/dev/null || true
    printf '%s\n' "$REGION" >"$REGION_CACHE" 2>/dev/null || true
    ;;
esac

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
#
# THIS GAUGE MEASURES SPACE, AND SPACE IS NOT THE ONLY WAY A DISK TAKES A HOST DOWN. On
# 2026-09-04 Gerrit was completely unreachable for 41 minutes with root at 47% FULL: the
# volume was IOPS-saturated, pinned flat at ~2,580 read IOPS (86% of its provisioned gp3
# 3,000) for thirty minutes, while this gauge read healthy and the 85% threshold was nowhere
# near tripping. Epic 6202 bounds disk SPACE — overlay2, BuildKit cache, journald, /var/tmp,
# a dedicated scratch volume — and NONE of that would have prevented or detected it. A future
# reader must not infer IOPS protection from the presence of these space metrics and caps;
# there is no alarm on the dimension that actually saturated (bug 1205-63b2-2c01-4e7f).
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
#
# GATED ON MOUNTEDNESS, and that gate is not defensive tidiness. `df` on an unmounted mount
# point silently answers for the filesystem CONTAINING it — root — so an unmounted scratch
# volume would publish ROOT's usage under mount=<scratch>. rebar-gate-scratch-disk-high would
# then read a number that is real but about the wrong volume: healthy while scratch is gone,
# or paging "scratch is full" during a root incident. Silence is the honest reading here, and
# it is not a blind spot — treat_missing_data = "breaching" pages on it, and
# rebar-gate-scratch-unmounted above names the actual condition.
scratch_pct=""
if [ "$scratch_mounted" -eq 1 ]; then
  scratch_pct=$(df --output=pcent "$GATE_SCRATCH_MOUNT" 2>/dev/null | tail -1 | tr -dc '0-9')
fi
if [ -n "$scratch_pct" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name disk_used_percent --unit Percent --value "$scratch_pct" \
    --dimensions InstanceId="$IID",mount="$GATE_SCRATCH_MOUNT" 2>/dev/null || true
  logger -t rebar-health "disk ${GATE_SCRATCH_MOUNT} used_percent=${scratch_pct}"
fi

# --- 2f. Docker storage GENERATORS (ADR 0112 decisions 1+2, story 9183) ------
# §2b answers "how full is root". It cannot answer "full OF WHAT", and on 2026-09-02 that
# gap cost five hours: `/var/lib/docker` was 17G of a 28G working set, `overlay2` alone 16G
# across 67 layer directories, and the only signal was "root disk high".
#
# THE DECISIVE PROBLEM, and why this section takes TWO measurements rather than one.
# `docker system df` reported ~9.5 GB with ZERO dangling images against that 16G of real
# overlay2 — roughly 6.5 GB was invisible to Docker's own accounting, so no `docker prune`
# could reach it (four rounds recovered ~1.06 GB against a 29 GB problem). A metric derived
# from the DAEMON'S LEDGER alone is therefore blind to exactly the bytes that caused the
# incident. So this publishes both halves and their difference:
#
#   filesystem truth  `du -sx` over ALL of /var/lib/docker. `-x` will not cross into a mounted
#                     overlay2/*/merged, and ONE `du` run counts a file hardlinked across
#                     layers once, so this is blocks actually consumed.
#   Docker's ledger   `docker system df`, ALL FOUR rows: Images + Containers + Build Cache +
#                     Local Volumes.
#   the residue       docker_unaccounted_bytes = root truth - whole ledger, clamped at 0.
#
# THE TWO SIDES MUST SPAN THE SAME BYTES, and getting that wrong is what code review caught on
# patchset 1. That revision differenced a `du` of `overlay2` ALONE against the ledger's Images
# + Containers + Build Cache, and excluded Local Volumes on the grounds that they live outside
# overlay2 — a subtrahend and a minuend over different byte sets, which under-reports the
# residue by whatever the ledger counts outside the minuend and can drive it to the clamp.
#
# The fix deliberately does NOT patch that by adding one more directory to the `du`. WHERE the
# daemon puts a given class of bytes is an implementation detail that MOVES: with the classic
# overlay2 graphdriver, BuildKit's own snapshots are backed by the daemon's layer store and so
# land in `overlay2` (moby builder/builder-next/controller.go passes `GraphDriver` and
# `LayerStore` into `snapshot.NewSnapshotter`, while `.../buildkit/` holds only the content
# store and the metadata DBs) — but with the containerd snapshotter enabled those same bytes
# live under `.../containerd/` and `overlay2` goes stale — the old layers stay on disk but
# stop growing — which would peg an overlay2-based residue at the clamp forever on a box that
# is filling. A metric whose correctness depends on
# a storage-driver detail it never checks is exactly the silent-healthy failure this story
# exists to remove.
#
# So BOTH sides are widened to the same, layout-independent set: every byte under the Docker
# root against every byte the daemon's ledger accounts for. Whichever subdirectory a given
# engine chooses, the difference stays the answer to one question — how much of what dockerd
# is storing does dockerd itself not know about.
#
# Widening the minuend also brings in bytes NO ledger row covers at all, which is a feature
# rather than a side effect: container JSON logs under `.../containers/<id>/`, BuildKit's
# content store under `.../buildkit/content/` (the one routinely-GB thing in that directory,
# and a known accumulator because the per-layer cleanup after a pull is disabled upstream),
# and the layer metadata under `.../image/`. Every one of those can fill a root volume while
# `docker system df` reports a comfortable total, which is the exact failure the 2026-09-02
# outage was.
#
# The one place the two sides can still part company is a Docker root SPLIT ACROSS MOUNTS:
# `-x` stops the `du` at a mount boundary, while the ledger keeps counting, so a separately
# mounted `.../volumes` would under-report the residue. That direction is the safe one — it
# loses sensitivity rather than inventing a page — and it is not this box, whose Docker root
# is entirely on the root volume.
#
# Some divergence is NORMAL — `du` counts allocated blocks including per-layer directory and
# whiteout overhead plus the daemon's own metadata (image/, network/, buildkit/*.db, tmp/),
# while the ledger reports layer sizes with sharing accounted differently — so the alarm
# threshold (monitoring_autodeploy.tf) is 2 GiB, far above that overhead (hundreds of MB on
# this box) and far below the 6.5 GB that went unnoticed.
#
# EVERY reading is GATED ON ITS OWN MEASUREMENT SUCCEEDING, the §2e rule: a probe that could
# not measure publishes NOTHING rather than a plausible 0. All three alarms are
# treat_missing_data = "breaching" (bug 3276 defect 2), so silence PAGES — while a fabricated
# 0 would read as a healthy, empty Docker root on a box that is filling. The overlay2 `du` is
# a DIAGNOSTIC BREADCRUMB only — it names the subtree in the log line and nothing is derived
# from it — so its failing mutes nothing.
#
# DIMENSIONLESS on both sides, following root_disk_used_percent (§2b): CloudWatch keys a
# metric by namespace+name+dimensions, so a dimension on only one side silently never matches.
#
# The caps come from infra/scripts/docker-storage-cap.sh, the single source of truth that also
# renders the daemon's own builder.gc policy — so the published "percent of cap" and the cap
# the daemon actually enforces can never drift apart.
DOCKER_CAP_SH="${DOCKER_CAP_SH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-storage-cap.sh}"
eval "$(bash "$DOCKER_CAP_SH" --print-env 2>/dev/null)" || true
DOCKER_ROOT="${DOCKER_ROOT:-/var/lib/docker}"
# Both docker calls are BOUNDED, the §2d rule: a wedged daemon under disk pressure must not
# hold the 5-minute timer open. `du` over a large overlay2 is a stat walk, hence the longer
# ceiling; exceeding it is indistinguishable from a failed read and is reported as silence.
#
# 60, not the former 120 (bug 9313-1fac-9f32-4b07). The 120 was sized for a shape that walked
# the tree TWICE, and two of them exactly equalled the unit's TimeoutStartSec=240. There is now
# one walk (docker_du_census), and the ceiling is set from what the walk actually does on this
# host rather than from hope: measured on the production Gerrit host at load average 3.45, the walk did
# not finish at a 95 s bound or at a 125 s bound, twice each, while a cache-warm walk completes
# in well under a minute (breadcrumbs at 17:48, 17:58, 18:03, 18:09, 18:42 on 2026-09-05). The
# reading is therefore bimodal — fast, or unobtainable — and a 120 s attempt that fails costs a
# further minute of the probe's budget over a 60 s attempt that fails, for no reading either
# way. 60 captures the fast mode and fails fast in the stalled one, where the §2e rule applies:
# a measurement that could not be taken is published as silence, and docker_du_seconds plus
# probe_elapsed_seconds now say WHY. These are ceilings, not budgets — both calls also pass
# through `clamped`, so neither can spend what a later section is owed.
#
# THE ACCEPTED COST, recorded so it is not re-investigated as a regression: under load this
# metric now goes intermittently silent, because the walk does not finish at ANY bound tried
# (95 s and 125 s both exhausted, twice each). A gap here with docker_du_seconds at ~60 and
# probe_ok=1 is this trade working as designed, NOT a truncated run. The trade, the options for
# actually recovering the reading, and the alarm treatment it needs are bug
# 5993-4cf7-0de9-4f72.
DOCKER_DU_TIMEOUT="${DOCKER_DU_TIMEOUT:-60}"
DOCKER_DF_TIMEOUT="${DOCKER_DF_TIMEOUT:-15}"

# --- BOUNDED JOURNAL COUNTING (bug 1205-63b2-2c01-4e7f) --------------------
# Every marker counter below turns journald into a per-interval delta. The ORIGINAL shape
# persisted a cumulative COUNT and published `total - prev`, and a cumulative total that is
# RECOMPUTED can only be recomputed by reading from the start of the journal. So each counter
# re-read the ENTIRE retained journal, twelve times per run across all of them.
#
# On 2026-09-04 that took Gerrit off the air for 41 minutes. Measured on a gp3-throttled corpus,
# twelve unbounded scans exhaust this timer's 300-second period at ~1.37 GB of journal and the
# host's real journal is 1.7 GB; the same journal read `-n 5000` instead of unbounded is 42.5x
# faster. The journald field index does NOT reduce the bytes read — journalctl still traverses
# every journal file — so an indexed matcher is not a bound.
#
# THE FIX IS NOT A `timeout` BOLTED ON. A truncated scan yields a truncated count, and
# `total - prev` then publishes a PLAUSIBLE WRONG NUMBER, which is worse than publishing none:
# an alarm cannot tell it from a healthy reading. `--since` alone is equally wrong — it turns
# `total` into a window count that the same subtraction renders meaningless.
#
# So the total is no longer RECOMPUTED, it is MAINTAINED. Each counter persists a journald
# CURSOR beside its total and reads only the entries after it, which is what journald's
# `--after-cursor` exists for. Read volume becomes a function of the INTERVAL rather than of how
# much journal the host retains, so it no longer grows as the box ages.
#
# STATE FILE: one file per counter holding `<total> <cursor>`, written temp-file-then-rename.
# The pair must be committed atomically because TimeoutStartSec (install-observability.sh) can
# now SIGKILL a run at any point, and a half-written pair is exactly the plausible wrong number
# above. The format is also ROLLBACK-SAFE: the pre-fix reader tested
# `case "$prev" in ''|*[!0-9]*)` and so classifies a two-field file as unreadable and reseeds,
# publishing 0 rather than a bogus delta.
#
# READING ONE STREAM: `-o cat --show-cursor` emits the entries followed by a single
# `-- cursor: <c>` line. That line is REMOVED before counting. This is not decoration — the g2p
# counter (§4c) is a free-form phrase match rather than a record anchor, and would otherwise be
# able to match cursor metadata.
#
# THE STATE LEGS, all of them:
#   cold start (no file)        seed the cursor from the tail, publish 0. Inherited history
#                               predates monitoring (bug e2a6-9ee4-8d5c-4290).
#   upgrade (bare total)        identical to cold start for the cursor, so the FIRST run after
#                               this change lands neither retro-counts the retention window nor
#                               performs one last full scan. The total carries forward.
#   nothing new                 an empty stream carries no cursor line: retain both fields
#                               unchanged and publish 0. The common case.
#   unusable cursor             journald rotated past it. A cheap bounded TAIL read
#                               discriminates this from a wedged journal: if the tail read
#                               works the journal is healthy and only the cursor is a casualty,
#                               so reseed from the tail. Publishing nothing beats the tempting
#                               recovery of re-reading from the beginning, which would reinstate
#                               this very defect at the moment the journal is largest.
#   unreadable                  neither field advances and nothing is published.
#
# PUBLISHING NOTHING is the honest value for an unmeasurable COUNTER. The pessimistic-value
# convention the gauges use (§2c) has no analogue here: 0 reads as healthy and any positive
# number is invented. The run still publishes its heartbeat gauges, so "the probe is alive" is
# still signalled and the absence is scoped to the one metric that could not be measured.
#
# The scan is wall-clock bounded as well. That bound is now applied through `clamped`, not
# `bounded`: this section's original argument — "12 scans x 10 s = 120 s < the 240 s
# TimeoutStartSec" — was sound in isolation and wrong in composition, because the docker walk
# above was separately entitled to 240 s of the same budget and nothing reconciled the two
# claims (bug 9313-1fac-9f32-4b07). `clamped` reconciles them in one place.
# mechanism-ok: env_var JOURNAL_SCAN_TIMEOUT — 1205-63b2-2c01-4e7f: the §2d wall-clock bound on
# every journald read, overridable only so the tests can drive the timeout path.
JOURNAL_SCAN_TIMEOUT="${JOURNAL_SCAN_TIMEOUT:-10}"

# `timeout` is coreutils and is present on the deployment host, but not on every host this
# script is exercised on. When it is missing the wall-clock BELT is skipped, not the command:
# the cursor is what removes the unbounded read, and refusing to run without `timeout` would
# take these metrics off the air on a host where nothing is wrong. `bounded <secs> <cmd...>`
# therefore degrades to running the command directly, and callers keep treating a non-zero exit
# as "this interval could not be counted" either way.
if command -v timeout >/dev/null 2>&1; then
  bounded() { timeout "$@"; }  # composition-door: the only raw `timeout` in this script
else
  bounded() { shift; "$@"; }
fi

# `bounded` with the whole-probe budget applied on top: the call gets the SMALLER of its own
# ceiling and what `probe_budget_left` still allows. Every wall-clock-bounded call in this
# script goes through here, so the composed worst case of all of them is the deadline rather
# than the sum of their independent ceilings (bug 9313-1fac-9f32-4b07).
# A cap-compliance HEARTBEAT's value, with THREE outcomes rather than two.
#
# `--check-active` / `--check-quota` print exactly `1` or `0`. Anything else — an empty string
# from a script that could not be executed, a non-zero exit, a truncated read — is UNKNOWN, and
# the old `case "$x" in 1) ;; *) x=0` coerced every one of those to a confident `0`. Those are
# different claims: `0` asserts the mechanism was MEASURED and is not in force, while silence
# from the check means nobody looked. Reporting the second as the first is what let
# `var_tmp_cleanup_active` and `container_reaper_active` read "the reapers are dead" for hours
# on a box where the reaper units did not exist at all (bug 5fb0-89ab-4466-41cc).
#
# The distinction is carried IN THE VALUE, not by withholding it. Every heartbeat still
# publishes on EVERY tick including its 0 path, because bug bff5-9163-cddd-4158 reserves ABSENCE
# to mean "the publisher died" — under treat_missing_data = "breaching" that is what makes the
# dead-man construction trustworthy, and spending it on "healthy but unknown" would make every
# dead-man alarm on this box ambiguous to buy a local readability win. The three outcomes follow
# scripts/assert_volumes_in_service.py (dcc3), where UNKNOWN stays distinct from NOT-IN-SERVICE
# and both fail.
#
# -1 pages IDENTICALLY to 0: every alarm on these metrics is `LessThanThreshold 1.0`, so the
# sentinel is below the threshold by construction and no alarm needs retuning to keep catching
# it. It reads honestly to an operator instead of asserting a measurement that never happened.
HEARTBEAT_UNKNOWN=-1

heartbeat_value() {
  case "$1" in
    1) printf '1\n' ;;
    0) printf '0\n' ;;
    *) printf '%s\n' "$HEARTBEAT_UNKNOWN" ;;
  esac
}

# The cap script a metrics section must EXECUTE, resolved from an ordered candidate list.
#
# The probe is installed as /usr/local/bin/rebar-observability.sh, so a bare sibling name
# resolves into /usr/local/bin — a directory only SOME cap scripts ever occupy.
# `vartmp-cap.sh` and `container-cap.sh` self-install under a `rebar-` PREFIXED name (that path
# is the ExecStart of the reaper units they write), so the sibling name is one nothing ever
# creates: executing it fails with rc 127, which takes the gated percent metric off the air AND
# pins the ungated heartbeat to a confident, false 0 (bug 5fb0-89ab-4466-41cc). Proven on the
# host: with both reaper timers installed and genuinely running, the probe still published 0.
#
# The SIBLING is tried FIRST so this is strictly additive — wherever the old single-candidate
# path existed it still wins, which keeps the checkout layout the tests drive unchanged — and
# the installed name is a FALLBACK reached only when the sibling is absent. Exactly one copy of
# each script therefore has to exist on the box; nothing is duplicated to satisfy the lookup.
#
# When no candidate exists the LAST is returned rather than the empty string, so the failure
# still names a path an operator can act on instead of `bash: : No such file or directory`.
resolve_cap_sh() {
  local candidate=""
  for candidate in "$@"; do
    if [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf '%s\n' "$candidate"
}

clamped() {
  local want="$1" left
  shift
  left="$(probe_budget_left)"
  [ "$want" -gt "$left" ] && want="$left"
  bounded "$want" "$@"  # composition-door: the only `bounded` call in this script
}

# The journal's tail cursor, into JOURNAL_TAIL_CURSOR. `-n 1` is a seek to the end rather than
# a traversal, so this costs one entry however large the journal. The RETURN STATUS is
# journalctl's, kept separate from the cursor because they answer different questions: a status
# of 0 with an empty cursor means the journal is readable but has no entries yet (a genuine
# cold start on a fresh box), while a non-zero status means it could not be read at all.
journal_tail_cursor() {
  local out rc
  out="$(clamped "$JOURNAL_SCAN_TIMEOUT" journalctl "$@" --no-pager -o cat -n 1 \
    --show-cursor 2>/dev/null)"
  rc=$?
  JOURNAL_TAIL_CURSOR="$(printf '%s\n' "$out" | sed -n 's/^-- cursor: //p' | tail -1)"
  return $rc
}

# Commit the (total, cursor) pair as one unit. A partial write would desynchronise them, and
# TimeoutStartSec can now SIGKILL this script at any point.
journal_state_write() {
  local file="$1" total="$2" cursor="$3" tmp
  mkdir -p "$(dirname "$file")" 2>/dev/null || true
  tmp="${file}.tmp.$$"
  printf '%s %s\n' "$total" "$cursor" >"$tmp" 2>/dev/null || return 1
  mv -f "$tmp" "$file" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 1; }
}

# Split one journalctl stream into JOURNAL_STREAM_COUNT (the matches) and
# JOURNAL_STREAM_CURSOR (the `-- cursor:` trailer). Taking the trailer out before counting is
# not decoration: the g2p counter (§4c) is a free-form phrase match rather than a record anchor
# and could otherwise match cursor metadata.
#
# Results come back in globals rather than on stdout ON PURPOSE. A command substitution runs its
# body in a SUBSHELL, so a function that returns one value on stdout can never also publish the
# other through a variable — the assignment would be discarded with the subshell, and the caller
# would silently keep reusing its previous cursor and recount the same entries every run.
journal_count_stream() {
  local stream="$1" flags="$2" pattern="$3" body
  JOURNAL_STREAM_CURSOR="$(printf '%s\n' "$stream" | sed -n 's/^-- cursor: //p' | tail -1)"
  body="$(printf '%s\n' "$stream" | grep -v '^-- cursor: ')" || true
  JOURNAL_STREAM_COUNT="$(printf '%s\n' "$body" | grep -c $flags -- "$pattern")" || true
  case "$JOURNAL_STREAM_COUNT" in '' | *[!0-9]*) JOURNAL_STREAM_COUNT=0 ;; esac
}

# journal_marker_delta <state_file> <grep_flags> <pattern> [journalctl selectors...]
#
# Sets JOURNAL_DELTA / JOURNAL_NEXT_TOTAL / JOURNAL_NEXT_CURSOR and returns 0 when the interval
# was measured; returns non-zero when it was NOT, in which case the caller publishes nothing.
# The caller commits the new state only after a successful publish, which preserves the existing
# publish-then-persist ordering: a failed CloudWatch call must leave the delta to be republished
# next tick rather than swallowing it (bug 6a65-*).
journal_marker_delta() {
  local state_file="$1" flags="$2" pattern="$3"
  shift 3
  local raw prev_total cursor out rc
  JOURNAL_DELTA=0
  JOURNAL_NEXT_TOTAL=0
  JOURNAL_NEXT_CURSOR=""

  raw="$(head -n 1 "$state_file" 2>/dev/null || true)"
  read -r prev_total cursor <<<"$raw"
  case "${prev_total:-}" in '' | *[!0-9]*) prev_total=0 ;; esac
  cursor="${cursor:-}"
  JOURNAL_NEXT_TOTAL="$prev_total"

  if [ -z "$cursor" ]; then
    # Cold start, or the upgrade from the pre-1205 bare-total format. Both seed and publish 0,
    # so the first run after this lands neither retro-counts the retention window nor performs
    # one last full scan. An empty journal seeds an empty cursor and simply tries again.
    journal_tail_cursor "$@" || return 1
    JOURNAL_NEXT_CURSOR="$JOURNAL_TAIL_CURSOR"
    return 0
  fi

  out="$(clamped "$JOURNAL_SCAN_TIMEOUT" journalctl "$@" --no-pager -o cat \
    --after-cursor "$cursor" --show-cursor 2>/dev/null)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # Either the journal cannot be read at all, or journald rotated past this cursor. A cheap
    # bounded tail read tells them apart. Either way this interval is unmeasured and nothing is
    # published, but when the journal itself is healthy the cursor is RESEEDED so an entry
    # journald no longer holds cannot stall the counter forever. The tempting alternative —
    # giving up on the cursor and re-reading from the beginning — is the defect this change
    # removes, and it would fire exactly when the journal is largest.
    journal_tail_cursor "$@" || return 1
    [ -n "$JOURNAL_TAIL_CURSOR" ] &&
      journal_state_write "$state_file" "$prev_total" "$JOURNAL_TAIL_CURSOR"
    return 1
  fi

  journal_count_stream "$out" "$flags" "$pattern"
  JOURNAL_DELTA="$JOURNAL_STREAM_COUNT"
  # An empty stream carries no cursor line: nothing arrived, so the old cursor still stands.
  JOURNAL_NEXT_CURSOR="${JOURNAL_STREAM_CURSOR:-$cursor}"
  JOURNAL_NEXT_TOTAL=$((prev_total + JOURNAL_DELTA))
  return 0
}

# Percent of a cap, NOT clamped — shared by 2f, 2g, 2h and 2i. Every metric derived through
# here exists to say whether a cap is HOLDING, so the one reading it must be able to produce is
# a value over 100. This used to clamp (bug b380-3dfc-99fc-4a0e): on 2026-09-05 the build cache
# sat at 5.875 GB against a 5.00 GiB `builder.gc.maxUsedSpace` — ~109% — and
# docker_buildkit_cache_used_percent published 100, which the operator read as the cap pinning
# the cache rather than as a half-gigabyte breach. Clamping is fine for a gauge whose semantics
# stop at full (a disk cannot exceed its own size); these are BUDGETS, where over is not an
# impossible state but the specific failure being watched for — `builder.gc.maxUsedSpace`,
# `SystemMaxUse` and the /var/tmp and container-layer shares are all best-effort targets their
# writers routinely exceed. The companion `*_bytes` gauges carry the magnitude, but nothing
# alarms on those, and an operator comparing a percentage to a threshold cannot see a ceiling
# that is applied silently. CloudWatch's `Percent` unit is a LABEL on a double, not a validated
# range: it accepts 109 and the >85 alarms keep firing either way.
pct_of_cap() {
  printf '%s\n' "$(( $1 * 100 / $2 ))"
}

# ONE traversal, BOTH readings (bug 9313-1fac-9f32-4b07). Sets DOCKER_DU_TOTAL (blocks under
# the whole root) and DOCKER_DU_OVERLAY2 (the overlay2 subtotal, "" when that child was not in
# the listing); non-zero when nothing parseable came back.
#
# WHAT THIS REPLACES, AND WHY IT WAS THE TIMEOUT. The previous shape called `du -sx` TWICE —
# once over $DOCKER_ROOT and once over $DOCKER_ROOT/overlay2, which on this host is
# essentially the same ~1.88M-file tree walked a second time. Each was bounded at
# DOCKER_DU_TIMEOUT, whose default was 120, and 2 x 120 is EXACTLY the unit's
# TimeoutStartSec=240: under I/O contention both walks hit their ceiling and systemd
# SIGTERM-ed the probe with nothing left for any section after this one. Measured on
# the production Gerrit host on 2026-09-05, a single walk took 124.18 s against a 130 s bound (i.e. it
# did not finish) while the whole run consumed ~35 s of CPU. The walk BLOCKS; it does not
# compute — which is why neither a longer timeout (it only moves the kill point) nor a faster
# script (there is no CPU to save) is the fix. The unit compounds it deliberately:
# install-observability.sh sets IOSchedulingClass=idle, so this walk is starved by design on a
# box with IOPS-saturation history.
#
# `--max-depth=1` yields every immediate child of the root AND the grand total from a single
# traversal, so the overlay2 subtotal now costs nothing. That reading was only ever a
# breadcrumb in the log line at §2f ("deliberately no longer participates in the arithmetic"),
# so the second walk was spending up to half of the probe's entire budget on a log message.
docker_du_census() {
  local root out parsed
  root="${1%/}"
  DOCKER_DU_TOTAL=""
  DOCKER_DU_OVERLAY2=""
  out="$(clamped "$DOCKER_DU_TIMEOUT" du -x --block-size=1 --max-depth=1 "$root" 2>/dev/null)" || return 1
  # The grand-total row is the one whose path IS the root; every other row is a child. Matching
  # on the path rather than on position keeps this correct whatever order the du implementation
  # emits, and a `du` that printed only the total (the -s shape) still parses.
  parsed="$(printf '%s\n' "$out" | awk -v root="$root" '
    $2 == root            { total = $1 }
    $2 == root "/overlay2" { overlay = $1 }
    END { if (total == "") exit 1; printf "%s %s\n", total, (overlay == "" ? "-" : overlay) }
  ')" || return 1
  # CROSS-DEVICE GUARD. `-x` is load-bearing for the total — it must not wander off the Docker
  # filesystem — but it also PRUNES at any mount boundary below the root, and a pruned directory
  # is still printed, as a stub holding only the mount point's own blocks. So if overlay2 were
  # ever given its own volume, the row above would parse cleanly and be wildly wrong. On this
  # host overlay2 shares the root device today (verified: same st_dev as /var/lib/docker), so
  # this is latent — but the capacity ticket 5993-4cf7-0de9-4f72 proposes a dedicated Docker
  # volume as one remedy, which would arm it. The predecessor shape did not have this problem
  # because it ran a SECOND `du -sx` starting AT overlay2, where `-x` anchors to that filesystem;
  # collapsing to one walk is what introduces it, so one walk has to carry the check.
  #
  # An O(1) st_dev comparison, and on a mismatch the subtotal is reported as UNKNOWN rather than
  # as a number that is silently a mount stub — the §2e rule. Only the log breadcrumb consumes
  # it, so nothing downstream degrades; a wrong value there would be worse than none.
  if [ -n "$DOCKER_DU_OVERLAY2_DEVCHECK_SKIP" ]; then
    :
  elif [ -d "$root/overlay2" ]; then
    root_dev="$(stat -c '%d' "$root" 2>/dev/null || stat -f '%d' "$root" 2>/dev/null || true)"
    ov_dev="$(stat -c '%d' "$root/overlay2" 2>/dev/null || stat -f '%d' "$root/overlay2" 2>/dev/null || true)"
    if [ -n "$root_dev" ] && [ -n "$ov_dev" ] && [ "$root_dev" != "$ov_dev" ]; then
      parsed="${parsed% *} -"
    fi
  fi
  read -r DOCKER_DU_TOTAL DOCKER_DU_OVERLAY2 <<CENSUS
$parsed
CENSUS
  case "$DOCKER_DU_TOTAL" in ''|*[!0-9]*) DOCKER_DU_TOTAL=""; return 1 ;; esac
  case "$DOCKER_DU_OVERLAY2" in *[!0-9]*) DOCKER_DU_OVERLAY2="" ;; esac
  return 0
}

# "<accounted_total> <build_cache> <containers> <containers_reclaimable>" in bytes from the
# daemon's ledger, or non-zero. The last two are -1 when this engine's rendering did not carry a
# parseable Containers row — §2i then publishes NOTHING rather than a plausible 0 (the §2e rule).
#
# ONE `docker system df` serves §2f and §2i. It is the expensive call in this probe (the daemon
# walks every layer to size it), so the Containers row is taken from the walk §2f already pays
# for rather than adding a second one.
# `docker system df` renders HUMAN sizes through go-units, which emits SI suffixes (kB/MB/GB)
# in some paths and binary ones (KiB/MiB/GiB) in others, so both families are parsed. An
# unrecognised suffix in a KNOWN row fails the whole read rather than silently contributing
# 0 — a ledger that under-reports would inflate the residue and page for bytes that are in
# fact accounted for.
#
# A row whose Type this does not recognise is IGNORED rather than fatal. `docker system df`
# is the daemon's own presentation layer and its row set has grown before; a future engine
# adding a fifth type in a rendering this cannot parse would otherwise silently take the
# incident metric off the air, which is the failure mode with the highest cost here. The
# residue then under-reports by that new row instead — visible, bounded, and recoverable.
docker_ledger_bytes() {
  local rows out
  rows="$(clamped "$DOCKER_DF_TIMEOUT" docker system df --format '{{.Type}}|{{.Size}}|{{.Reclaimable}}' 2>/dev/null)" || return 1
  [ -n "$rows" ] || return 1
  out="$(printf '%s\n' "$rows" | awk -F'|' '
    function tobytes(s,   n, u, m) {
      gsub(/^[ \t]+|[ \t]+$/, "", s)
      if (s ~ /^[0-9.]+$/) return s + 0
      if (s !~ /^[0-9.]+[A-Za-z]+$/) return -1
      n = s; sub(/[A-Za-z]+$/, "", n); n = n + 0
      u = s; sub(/^[0-9.]+/, "", u)
      if (u == "B") m = 1
      else if (u == "kB" || u == "KB") m = 1000
      else if (u == "MB") m = 1000000
      else if (u == "GB") m = 1000000000
      else if (u == "TB") m = 1000000000000
      else if (u == "KiB") m = 1024
      else if (u == "MiB") m = 1048576
      else if (u == "GiB") m = 1073741824
      else if (u == "TiB") m = 1099511627776
      else return -1
      return int(n * m + 0.5)
    }
    BEGIN { containers = -1; reclaimable = -1 }
    {
      type = $1; gsub(/^[ \t]+|[ \t]+$/, "", type)
      known = (type == "Images" || type == "Containers" || type == "Build Cache" || type == "Local Volumes")
      if (!known) next
      value = tobytes($2)
      if (value < 0) { bad = 1; next }
      total += value; seen = 1
      if (type == "Build Cache") cache = value
      if (type == "Containers") {
        containers = value
        # RECLAIMABLE renders as "1.2GB (100%)" — the parenthesised share is presentation, not a
        # quantity. An engine that emits no third field (or an unparseable one) leaves this -1,
        # which §2i reports as SILENCE; it never degrades to 0, which would read as "no
        # exited-container debris" on a box accumulating it.
        rc = $3
        sub(/[ \t]*\(.*$/, "", rc)
        if (rc != "") reclaimable = tobytes(rc)
      }
    }
    END {
      if (!seen || bad) exit 1
      printf "%d %d %d %d\n", total, cache, containers, reclaimable
    }
  ')" || return 1
  [ -n "$out" ] || return 1
  printf '%s\n' "$out"
}

docker_total_bytes=""
docker_overlay2_bytes=""
# ONE walk for both readings, and its cost is itself published (docker_du_seconds below) so the
# call that caused bug 9313-1fac-9f32-4b07 can be watched instead of re-measured by hand.
docker_du_started_at="$(date +%s)"
if docker_du_census "$DOCKER_ROOT"; then
  docker_total_bytes="$DOCKER_DU_TOTAL"
  docker_overlay2_bytes="$DOCKER_DU_OVERLAY2"
fi
docker_du_seconds=$(( $(date +%s) - docker_du_started_at ))
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name docker_du_seconds --unit Seconds --value "$docker_du_seconds" 2>/dev/null || true

if [ -n "$docker_total_bytes" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name docker_storage_bytes --unit Bytes --value "$docker_total_bytes" 2>/dev/null || true
  logger -t rebar-health "docker ${DOCKER_ROOT} bytes=${docker_total_bytes}"
  if [ -n "${DOCKER_BUDGET_BYTES:-}" ] && [ "${DOCKER_BUDGET_BYTES:-0}" -gt 0 ]; then
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name docker_storage_used_percent --unit Percent \
      --value "$(pct_of_cap "$docker_total_bytes" "$DOCKER_BUDGET_BYTES")" 2>/dev/null || true
  fi
fi

docker_ledger=""
docker_ledger="$(docker_ledger_bytes)" || docker_ledger=""
docker_container_bytes=""
docker_container_reclaimable=""
if [ -n "$docker_ledger" ]; then
  # `read`, not `set --`: this script's own positional parameters are not scratch space.
  read -r docker_accounted_bytes docker_cache_bytes _df_containers _df_reclaimable <<LEDGER
$docker_ledger
LEDGER
  [ "${_df_containers:--1}" -ge 0 ] && docker_container_bytes="$_df_containers"
  [ "${_df_reclaimable:--1}" -ge 0 ] && docker_container_reclaimable="$_df_reclaimable"
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name docker_buildkit_cache_bytes --unit Bytes --value "$docker_cache_bytes" 2>/dev/null || true
  logger -t rebar-health "docker buildkit cache bytes=${docker_cache_bytes} accounted=${docker_accounted_bytes}"
  if [ -n "${DOCKER_BUILDKIT_CACHE_BYTES:-}" ] && [ "${DOCKER_BUILDKIT_CACHE_BYTES:-0}" -gt 0 ]; then
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name docker_buildkit_cache_used_percent --unit Percent \
      --value "$(pct_of_cap "$docker_cache_bytes" "$DOCKER_BUILDKIT_CACHE_BYTES")" 2>/dev/null || true
  fi
  # The residue needs BOTH halves. Without either one there is no defensible number, so none
  # is invented — this is the one metric whose whole value is that it is not derivable from
  # Docker's own accounting. The filesystem half is the WHOLE Docker root, matching the whole
  # ledger above; the overlay2 reading is carried in the log line as the incident breadcrumb
  # (16G of the 17G on 2026-09-02) and deliberately no longer participates in the arithmetic.
  if [ -n "$docker_total_bytes" ]; then
    docker_unaccounted=$(( docker_total_bytes - docker_accounted_bytes ))
    # Clamped. The ledger can legitimately exceed the `du`: `docker system df` sums each row
    # independently, so a build-cache record that SHARES its layer with an image is counted
    # twice where `du` counts those blocks once (CLI <= v28 subtracted shared records from the
    # Build Cache column; v29 stopped, so the overlap is version-dependent). A negative
    # datapoint against a GreaterThanThreshold alarm reads as reassuring, which is worse than
    # nonsense, so the floor is 0 — under-reporting, never a false all-clear that looks precise.
    [ "$docker_unaccounted" -lt 0 ] && docker_unaccounted=0
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name docker_unaccounted_bytes --unit Bytes --value "$docker_unaccounted" 2>/dev/null || true
    logger -t rebar-health \
      "docker root bytes=${docker_total_bytes} overlay2=${docker_overlay2_bytes:-unread} ledger=${docker_accounted_bytes} unaccounted=${docker_unaccounted} (bytes docker prune cannot reach)"
  fi
fi

# --- 2g. journald, the /var/log generator (ADR 0112 decisions 1+2, story e956) ------
# §2b answers "how full is root". §2f named the Docker accumulator; this names the other one
# the 2026-09-02 measurement found: /var/log was 1.8G of the 28G working set and 1.7G of that
# was the JOURNAL, because every compose service logs to the host journal.
#
# THREE readings, and they fail independently on purpose.
#
#   journal_bytes          the size of ${JOURNAL_DIR}, from the filesystem.
#   journal_used_percent   that size against the ceiling journald is configured to enforce.
#   journal_cap_in_effect  a 1/0 HEARTBEAT: is that ceiling the one the LIVE journald read?
#
# BOTH SIDES OF THE RATIO MUST SPAN THE SAME BYTES — §2f's lesson in ratio form. `SystemMaxUse`
# governs the journal files under ${JOURNAL_DIR} and nothing else, so the numerator is a `du`
# of exactly that tree. Measuring /var/log instead would count rotated syslog, nginx access
# logs and every other consumer against a ceiling that does not bound them, and the percentage
# would be about no quantity at all. The one residual runs the safe way: `du` counts every byte
# under the tree while journald's quota counts only its own `*.journal*` files, so a stray file
# there OVER-reports and pages early rather than reading healthy.
#
# THE HEARTBEAT IS NOT A SPARE METRIC. journald reads its configuration at startup and
# systemd-journald.service implements no ExecReload, so a ceiling can sit on disk while the
# running daemon enforces the one it read at boot — and in that state `journal_used_percent` is
# computed against a denominator that is NOT in force, so every other reading looks healthy.
# Story 9183 shipped exactly that gap for the BuildKit share ("the runbook documents the
# restart; nothing tracks that it happened"). This tracks it. It follows the §2e heartbeat rule
# (bug bff5): a value on EVERY tick INCLUDING the 0 path, so ABSENCE means the probe, the timer
# or the host is dead rather than the ceiling being fine.
#
# Every other reading is GATED ON ITS OWN MEASUREMENT SUCCEEDING, the §2e/§2f rule: a probe that
# could not measure publishes NOTHING rather than a plausible 0, and treat_missing_data =
# "breaching" pages on the silence — while a 0 would read as an empty journal on a box that is
# filling.
#
# DIMENSIONLESS on both sides, following root_disk_used_percent (§2b): CloudWatch keys a metric
# by namespace+name+dimensions, so a dimension on only one side silently never matches.
#
# The ceiling comes from infra/scripts/journald-cap.sh, the single source of truth that also
# renders the drop-in journald reads — so the published "percent of cap" and the cap the box is
# configured to enforce can never drift apart.
JOURNALD_CAP_SH="${JOURNALD_CAP_SH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/journald-cap.sh}"
eval "$(bash "$JOURNALD_CAP_SH" --print-env 2>/dev/null)" || true
JOURNAL_DIR="${JOURNAL_DIR:-/var/log/journal}"
# BOUNDED, the §2d rule: a `du` over a large journal is a stat walk and must not hold the
# 5-minute timer open. Exceeding it is indistinguishable from a failed read, and is reported as
# silence.
JOURNAL_DU_TIMEOUT="${JOURNAL_DU_TIMEOUT:-60}"

journal_in_effect="$(heartbeat_value "$(bash "$JOURNALD_CAP_SH" --check-active 2>/dev/null)")"
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name journal_cap_in_effect --unit Count --value "$journal_in_effect" 2>/dev/null || true
logger -t rebar-health "journal ceiling ${JOURNAL_MAX_USE_BYTES:-unset}B in_effect=${journal_in_effect}"
if [ "$journal_in_effect" -eq 0 ]; then
  logger -t rebar-health \
    "the journald ceiling is NOT the one the running systemd-journald read — journal_used_percent is measured against a cap that is not in force; see infra/runbooks/review-bot-ops.md"
elif [ "$journal_in_effect" -lt 0 ]; then
  logger -t rebar-health \
    "could NOT determine whether the journald ceiling is in force — ${JOURNALD_CAP_SH} did not answer; this is an unmeasured state, not a cap known to be absent; see infra/runbooks/review-bot-ops.md"
fi

journal_bytes="$(clamped "$JOURNAL_DU_TIMEOUT" du -sx --block-size=1 "$JOURNAL_DIR" 2>/dev/null | tail -1 | awk '{print $1}')"
case "$journal_bytes" in ''|*[!0-9]*) journal_bytes="" ;; esac
if [ -n "$journal_bytes" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name journal_bytes --unit Bytes --value "$journal_bytes" 2>/dev/null || true
  logger -t rebar-health "journal ${JOURNAL_DIR} bytes=${journal_bytes}"
  # Independently gated from the size: losing the ceiling must not also take the MAGNITUDE off
  # the air, since journal_bytes is what an operator sizes the problem with.
  if [ -n "${JOURNAL_MAX_USE_BYTES:-}" ] && [ "${JOURNAL_MAX_USE_BYTES:-0}" -gt 0 ]; then
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name journal_used_percent --unit Percent \
      --value "$(pct_of_cap "$journal_bytes" "$JOURNAL_MAX_USE_BYTES")" 2>/dev/null || true
  fi
fi

# --- 2h. /var/tmp, the fourth root generator (ADR 0112 decisions 1+2, story 2ba3) ------
# §2b answers "how full is root". §2f named the Docker accumulator and §2g the journal; this
# names the last one the 2026-09-02 measurement found: /var/tmp was 3.6G of the 28G working set,
# accreted job scratch that nothing observed and nothing bounded.
#
# FOUR readings, and they fail independently on purpose.
#
#   var_tmp_bytes                the size of ${VAR_TMP_DIR}, from the filesystem.
#   var_tmp_used_percent         that size against the byte budget the box is configured to hold.
#   var_tmp_cleanup_active       1/0 HEARTBEAT: is anything bounding this tree at all?
#   var_tmp_hard_quota_in_effect 1/0 HEARTBEAT: is that bound a CEILING or a mitigation?
#
# THE FOURTH READING IS THE POINT OF THIS SECTION, and it is what makes §2h different from §2g.
# journald's SystemMaxUse is a real cap the writer checks as it extends a file, so §2g's
# heartbeat only has to answer "did the daemon read the file". /var/tmp has NO such writer: it is
# an ordinary directory on the root XFS filesystem, systemd-tmpfiles bounds AGE and never BYTES,
# and the one true byte ceiling — an XFS project quota — cannot be turned on here without
# rootflags=pquota on the kernel command line and a REBOOT of this host. So the box normally runs
# on a timer-driven oldest-first reaper, which is a MITIGATION WITH A FILL-RATE ASSUMPTION: at
# the 4 GiB default and a 300 s period, a sustained net fill above ~14.6 MB/s exceeds the budget
# before the reaper next runs, and this gp3 volume does 125 MB/s. Publishing which regime is live
# means "bounded" is a reading rather than a claim a runbook makes on the box's behalf — the
# exact confusion this epic exists to remove.
#
# BOTH SIDES OF THE RATIO SPAN THE SAME BYTES, §2f/§2g's rule: the budget is about /var/tmp, so
# the numerator is a `du` of exactly that tree. Measuring /var would count the Gerrit site tree
# and the whole Docker root against a ceiling that bounds neither.
#
# Every non-heartbeat reading is GATED ON ITS OWN MEASUREMENT SUCCEEDING, the §2e/§2f/§2g rule: a
# probe that could not measure publishes NOTHING rather than a plausible 0, and
# treat_missing_data = "breaching" pages on the silence — while a 0 would read as an empty
# /var/tmp on a box that is filling. The heartbeats are the deliberate exception (bug bff5): a
# value on EVERY tick INCLUDING the 0 path, so ABSENCE means the probe, the timer or the host is
# dead rather than the cleanup being fine.
#
# DIMENSIONLESS on both sides, following root_disk_used_percent (§2b): CloudWatch keys a metric
# by namespace+name+dimensions, so a dimension on only one side silently never matches.
#
# The budget comes from infra/scripts/vartmp-cap.sh, the single source of truth that also renders
# the tmpfiles drop-in and the reaper units — so the published "percent of cap" and the budget
# the box is configured to hold can never drift apart.
VARTMP_CAP_SH="${VARTMP_CAP_SH:-$(resolve_cap_sh "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vartmp-cap.sh" "${VAR_TMP_INSTALLED_PATH:-/usr/local/bin/rebar-vartmp-cap.sh}")}"
eval "$(bash "$VARTMP_CAP_SH" --print-env 2>/dev/null)" || true
VAR_TMP_DIR="${VAR_TMP_DIR:-/var/tmp}"
# BOUNDED through the same `bounded` wrapper the journal reads use, the §2d rule (bug 1205): a
# `du` over /var/tmp is a stat walk over a tree NOBODY PLANNED THE SIZE OF, which is the worst
# possible thing to run unbounded inside a 5-minute timer. Exceeding it is indistinguishable from
# a failed read and is reported as silence.
VAR_TMP_DU_TIMEOUT="${VAR_TMP_DU_TIMEOUT:-60}"

var_tmp_cleanup="$(heartbeat_value "$(bash "$VARTMP_CAP_SH" --check-active 2>/dev/null)")"
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name var_tmp_cleanup_active --unit Count --value "$var_tmp_cleanup" 2>/dev/null || true

var_tmp_quota="$(heartbeat_value "$(bash "$VARTMP_CAP_SH" --check-quota 2>/dev/null)")"
# Published WITHOUT an alarm, deliberately. The quota needs a host reboot to enable, so on this
# box the honest value is 0 for as long as that reboot has not been scheduled — an alarm on it
# would page continuously and be muted within a day, which is how a real signal becomes noise.
# It is a capacity FACT an operator reads when interpreting var_tmp_used_percent, not an incident.
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name var_tmp_hard_quota_in_effect --unit Count --value "$var_tmp_quota" 2>/dev/null || true
logger -t rebar-health \
  "var_tmp ${VAR_TMP_DIR} budget=${VAR_TMP_MAX_BYTES:-unset}B cleanup_active=${var_tmp_cleanup} hard_quota=${var_tmp_quota}"
if [ "$var_tmp_cleanup" -eq 0 ]; then
  logger -t rebar-health \
    "NOTHING is bounding ${VAR_TMP_DIR} — the tmpfiles drop-in is missing or stale, or rebar-var-tmp-reaper.timer is not running; see infra/runbooks/review-bot-ops.md"
elif [ "$var_tmp_cleanup" -lt 0 ]; then
  logger -t rebar-health \
    "could NOT determine whether anything bounds ${VAR_TMP_DIR} — ${VARTMP_CAP_SH} did not answer; this is an unmeasured state, not a bound known to be absent; see infra/runbooks/review-bot-ops.md"
fi

var_tmp_bytes="$(clamped "$VAR_TMP_DU_TIMEOUT" du -sx --block-size=1 "$VAR_TMP_DIR" 2>/dev/null | tail -1 | awk '{print $1}')"
case "$var_tmp_bytes" in ''|*[!0-9]*) var_tmp_bytes="" ;; esac
if [ -n "$var_tmp_bytes" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name var_tmp_bytes --unit Bytes --value "$var_tmp_bytes" 2>/dev/null || true
  logger -t rebar-health "var_tmp ${VAR_TMP_DIR} bytes=${var_tmp_bytes}"
  # Independently gated from the size: losing the budget must not also take the MAGNITUDE off the
  # air, since var_tmp_bytes is what an operator sizes the problem with.
  if [ -n "${VAR_TMP_MAX_BYTES:-}" ] && [ "${VAR_TMP_MAX_BYTES:-0}" -gt 0 ]; then
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name var_tmp_used_percent --unit Percent \
      --value "$(pct_of_cap "$var_tmp_bytes" "$VAR_TMP_MAX_BYTES")" 2>/dev/null || true
  fi
fi

# --- 2i. writable container layers, the last root generator (ADR 0112, story 910b) ------
# §2f named the Docker accumulator as a whole and capped its BuildKit share. This names what is
# INSIDE it that no image or build-cache prune can reach: each container's writable layer, the
# overlay2 `upperdir` it accumulates as it runs. On 2026-09-02 nothing measured them, so the
# only signal was "root disk high".
#
# FIVE readings, and they fail independently on purpose.
#
#   container_writable_bytes        every container's writable layer, running and exited.
#   container_exited_bytes          the subset belonging to STOPPED containers, i.e. the debris.
#   container_writable_used_percent the first of those against the share the box holds.
#   container_reaper_active         1/0 HEARTBEAT: is anything reaping that debris at all?
#   container_quota_enforceable     1/0: could a HARD per-container ceiling exist on this host?
#
# THE LAST TWO ARE NOT SPARE METRICS, and they answer different questions.
#
# `container_reaper_active` is the one that is ALARMED. A cap enforced by a timer with no
# liveness signal is a cap that can silently stop existing while every other reading stays
# nominal — usage sits at 40%, nothing is reaping, and the box looks healthy right up to the
# volume filling. It follows the §2e heartbeat rule (bug bff5): a value on EVERY tick INCLUDING
# the 0 path, so ABSENCE means the probe, the timer or the host is dead.
#
# `container_quota_enforceable` is published WITHOUT an alarm, deliberately, following
# var_tmp_hard_quota_in_effect (§2h). overlay2's per-container `--storage-opt size=` is refused
# unless the filesystem backing /var/lib/docker is XFS mounted with `pquota`, and XFS reads quota
# options at MOUNT time — so on this root filesystem it needs rootflags=pquota and a REBOOT, and
# the honest value is 0 until that reboot is scheduled. An alarm on it would page continuously
# and be muted within a day. It is the capacity FACT an operator reads when interpreting
# rebar-container-writable-usage-high, not an incident.
#
# READ THE PERCENTAGE KNOWING WHAT HOLDS IT. The reaper can only remove EXITED containers, so a
# RUNNING container's writable layer is bounded by NOTHING here — for the live compose set this
# is measurement and an alarm, not a ceiling. That is why both heartbeats exist rather than one.
#
# BOTH SIDES OF THE RATIO SPAN THE SAME BYTES, the §2f/§2g/§2h rule: the share is about writable
# layers, so the numerator is the daemon's own SizeRw sum (the Containers row of the same
# `docker system df` §2f already ran) and not a `du` of overlay2, which would count image layers
# against a share that does not bound them.
#
# Every non-heartbeat reading is GATED ON ITS OWN MEASUREMENT SUCCEEDING (§2e/§2f/§2g/§2h): a
# probe that could not measure publishes NOTHING, and treat_missing_data = "breaching" pages on
# the silence — while a 0 would read as "no writable layers at all" on a box that is filling.
#
# DIMENSIONLESS on both sides, following root_disk_used_percent (§2b): CloudWatch keys a metric
# by namespace+name+dimensions, so a dimension on only one side silently never matches.
#
# The share comes from infra/scripts/container-cap.sh, which reads it in turn from
# docker-storage-cap.sh — ONE budget with an internal split (ADR 0112), so the published
# percent-of-share and the share the reaper holds are the same number by construction.
CONTAINER_CAP_SH="${CONTAINER_CAP_SH:-$(resolve_cap_sh "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/container-cap.sh" "${CONTAINER_INSTALLED_PATH:-/usr/local/bin/rebar-container-cap.sh}")}"
eval "$(bash "$CONTAINER_CAP_SH" --print-env 2>/dev/null)" || true

container_reaper="$(heartbeat_value "$(bash "$CONTAINER_CAP_SH" --check-active 2>/dev/null)")"
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name container_reaper_active --unit Count --value "$container_reaper" 2>/dev/null || true

container_quota="$(heartbeat_value "$(bash "$CONTAINER_CAP_SH" --check-quota 2>/dev/null)")"
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name container_quota_enforceable --unit Count --value "$container_quota" 2>/dev/null || true
logger -t rebar-health \
  "container writable share=${CONTAINER_WRITABLE_BYTES:-unset}B reaper_active=${container_reaper} quota_enforceable=${container_quota}"
if [ "$container_reaper" -eq 0 ]; then
  logger -t rebar-health \
    "NOTHING is reaping exited-container debris — rebar-container-reaper.timer is not running or its units are stale; see infra/runbooks/review-bot-ops.md"
elif [ "$container_reaper" -lt 0 ]; then
  logger -t rebar-health \
    "could NOT determine whether anything reaps exited-container debris — ${CONTAINER_CAP_SH} did not answer; this is an unmeasured state, not a reaper known to be absent; see infra/runbooks/review-bot-ops.md"
fi

if [ -n "$docker_container_bytes" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name container_writable_bytes --unit Bytes --value "$docker_container_bytes" 2>/dev/null || true
  logger -t rebar-health "container writable layers bytes=${docker_container_bytes}"
  # Independently gated from the size: losing the share must not also take the MAGNITUDE off the
  # air, since container_writable_bytes is what an operator sizes the problem with.
  if [ -n "${CONTAINER_WRITABLE_BYTES:-}" ] && [ "${CONTAINER_WRITABLE_BYTES:-0}" -gt 0 ]; then
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name container_writable_used_percent --unit Percent \
      --value "$(pct_of_cap "$docker_container_bytes" "$CONTAINER_WRITABLE_BYTES")" 2>/dev/null || true
  fi
fi

# Published INDEPENDENTLY of the total above: the debris figure is the one that says whether the
# reaper has anything to work with, so an engine rendering that costs us the total must not also
# cost us the answer to "is this debris or is it the live services".
if [ -n "$docker_container_reclaimable" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name container_exited_bytes --unit Bytes --value "$docker_container_reclaimable" 2>/dev/null || true
  logger -t rebar-health "exited-container debris bytes=${docker_container_reclaimable}"
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
    # BOUNDED, the §2d rule (bug 1205-63b2-2c01-4e7f): debris is by definition something nobody
    # planned, so its size is unknown and this walks it inside the 5-minute probe.
    entry_kb=$(clamped "$JOURNAL_SCAN_TIMEOUT" du -sk "$entry" 2>/dev/null | tail -1 | awk '{print $1}')
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
container_ps=$(clamped 15 docker ps --no-trunc \
  --format 'PS|{{.Names}}|{{.Label "rebar.service"}}|{{.Label "com.docker.compose.service"}}' \
  2>/dev/null) || true
container_stats=$(clamped 15 docker stats --no-stream \
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
# Counted from the cursor, not from the start of the journal (bug 1205-63b2-2c01-4e7f).
# Published WITHOUT dimensions to match the dimensionless alarm in monitoring_s4b.tf
# (CloudWatch keys a metric by namespace+name+dimensions; the alarm has none).
if journal_marker_delta "$VOTER_OFFSET_FILE" -E '^VOTER_ERROR \{' \
  CONTAINER_NAME="$VOTER_CONTAINER"; then
  vnew="$JOURNAL_DELTA"
  if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name voter_errors --unit Count --value "$vnew" 2>/dev/null; then
    journal_state_write "$VOTER_OFFSET_FILE" "$JOURNAL_NEXT_TOTAL" \
      "$JOURNAL_NEXT_CURSOR"
  fi
  [ "$vnew" -gt 0 ] && logger -t rebar-health "review-bot voter failures (new this interval)=${vnew}"
else
  logger -t rebar-health "voter_errors NOT published: this interval could not be counted"
fi

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
if journal_marker_delta "$MERGE_OFFSET_FILE" -E '^MERGE_CHANGE_ERROR \{' \
  CONTAINER_NAME="$VOTER_CONTAINER"; then
  mnew="$JOURNAL_DELTA"
  if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name review_bot_merge_change_errors --unit Count --value "$mnew" 2>/dev/null; then
    journal_state_write "$MERGE_OFFSET_FILE" "$JOURNAL_NEXT_TOTAL" \
      "$JOURNAL_NEXT_CURSOR"
  fi
else
  mnew=0
  logger -t rebar-health \
    "review_bot_merge_change_errors NOT published: this interval could not be counted"
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
if journal_marker_delta "$DEPLOY_OFFSET_FILE" -E '^AUTODEPLOY_ERROR \{' \
  -u rebar-autodeploy.service; then
  dnew="$JOURNAL_DELTA"
  if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name deploy_errors --unit Count --value "$dnew" 2>/dev/null; then
    journal_state_write "$DEPLOY_OFFSET_FILE" "$JOURNAL_NEXT_TOTAL" \
      "$JOURNAL_NEXT_CURSOR"
  fi
  [ "$dnew" -gt 0 ] && logger -t rebar-health "auto-deploy failures (new this interval)=${dnew}"
else
  logger -t rebar-health "deploy_errors NOT published: this interval could not be counted"
fi


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
# One journal read per CALL, and this is called once per marker — which is what made the scan
# cost grow linearly with the number of published metrics (bug 1205). Each read is now scoped
# to the counter's own cursor, so nine call sites cost nine INTERVALS, not nine retentions.
publish_autodeploy_marker_delta() {
  local token="$1" metric="$2" offset_file="$3" label="$4" new
  if ! journal_marker_delta "$offset_file" -E "$token" -u rebar-autodeploy.service; then
    logger -t rebar-health "${metric} NOT published: this interval could not be counted"
    return 0
  fi
  new="$JOURNAL_DELTA"
  if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name "$metric" --unit Count --value "$new" 2>/dev/null; then
    journal_state_write "$offset_file" "$JOURNAL_NEXT_TOTAL" \
      "$JOURNAL_NEXT_CURSOR"
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
# This counter is a free-form PHRASE match rather than a record anchor, which is exactly why
# journal_marker_delta strips the `-- cursor:` line before counting (bug 1205).
# Published WITHOUT dimensions to match the dimensionless alarm in monitoring_1fa8.tf
# (CloudWatch keys a metric by namespace+name+dimensions; the alarm has none).
if journal_marker_delta "$G2P_OFFSET_FILE" -iE "$G2P_PATTERN" \
  CONTAINER_NAME="$G2P_CONTAINER"; then
  gnew="$JOURNAL_DELTA"
  if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name g2p_dispatch_errors --unit Count --value "$gnew" 2>/dev/null; then
    journal_state_write "$G2P_OFFSET_FILE" "$JOURNAL_NEXT_TOTAL" \
      "$JOURNAL_NEXT_CURSOR"
  fi
  [ "$gnew" -gt 0 ] && logger -t rebar-health "g2p CI-dispatch failures (new this interval)=${gnew}"
else
  logger -t rebar-health "g2p_dispatch_errors NOT published: this interval could not be counted"
fi

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
# alarm's 8-of-8 five-minute window (monitoring_ws7.tf) absorbs an isolated fetch blip: it takes
# EVERY datapoint in 40 minutes to be breaching to page, so a one-off curl timeout — or an
# ordinary publish gap — cannot reach it (ticket a9d1-c7f3-cfd9-44ff rewindowed that alarm).
GERRIT_BASE_URL="${GERRIT_BASE_URL:-https://rebar.solutions.navateam.com}"
GITHUB_REPO_URL="${GITHUB_REPO_URL:-https://github.com/navapbc/rebar}"
gerrit_sha=$(curl -fsS --max-time 10 "${GERRIT_BASE_URL}/projects/rebar/branches/main" 2>/dev/null \
  | sed "s/)]}'//" | grep -oE '"revision": ?"[0-9a-f]+"' | grep -oE '[0-9a-f]{40}')
# BOUNDED, the §2d rule (bug 1205-63b2-2c01-4e7f). This reaches GitHub over the network with no
# bound of its own, while the Gerrit REST read on the line above is correctly `--max-time 10`.
# An empty result takes the same "comparison could not be made" path as a failed curl, so the
# timeout degrades to the already-handled unknown case rather than to a wrong answer.
github_sha=$(clamped 15 git ls-remote "${GITHUB_REPO_URL}" refs/heads/main 2>/dev/null \
  | awk '{print $1}')
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

# --- COMPLETION HEARTBEAT (bug 9313-1fac-9f32-4b07) ------------------------
# The counterpart to probe_truncated above, and the reason a gap is now readable. probe_ok is
# published ONLY here, after every section has had its turn, so its presence means the whole
# script ran and its absence means the run did not reach the end. probe_elapsed_seconds
# carries how close that run came to TimeoutStartSec, which is the leading indicator this
# probe was missing: the timeouts were visible in `systemctl` all along and in no metric.
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name probe_elapsed_seconds --unit Seconds \
  --value "$(( $(date +%s) - PROBE_STARTED_AT ))" 2>/dev/null || true
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name probe_ok --unit Count --value 1 2>/dev/null || true

# Always exit success on a completed probe run. Without this, the script's exit
# status is that of its last statement — and every metric section ends in a
# `[ "$n" -gt 0 ] && logger …` guard that is *false* on a healthy box (n=0),
# making the whole probe exit 1 and marking the systemd oneshot `failed` (which
# trips the deploy/health alarms). The probe reports state via metrics/journald,
# not its exit code; a run that reached here completed successfully.
exit 0
