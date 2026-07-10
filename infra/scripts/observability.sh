#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# observability.sh — the rebar box's host observability probe (S2 + S5 + S4b + S7 + 1fa8).
#
# Run periodically by a systemd timer (install-observability.sh). Each run publishes
# CloudWatch metrics + journald log lines:
#   1. Health probe of Gerrit + the review-bot (/review/health) -> journald +
#      rebar/host:{gerrit_healthy,reviewbot_healthy} (S2).
#   2. Gerrit data-volume disk-used-percent -> rebar/host:disk_used_percent (S2 alarm).
#   3. Gerrit->GitHub replication failures (replication_log) -> rebar/host:replication_errors (S5 alarm).
#   4. review-bot voter failures (VOTER_ERROR in journald) -> rebar/host:voter_errors (S4b alarm).
#   4c. review-bot merge-change failures (MERGE_CHANGE_ERROR) -> rebar/host:review_bot_merge_change_errors (epic 88ab/S2 alarm).
#   4d. continuous auto-deploy failures (AUTODEPLOY_ERROR in the unit journal) -> rebar/host:deploy_errors (epic 88ab/8903 alarm).
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
  total=$(grep -cE 'REJECTED_NONFASTFORWARD|non-fast-forward|Giving up|giving up after|\[ERROR\]' "$REPL_LOG" 2>/dev/null) || true
  total=${total:-0}
  prev=$(cat "$REPL_OFFSET_FILE" 2>/dev/null || echo 0)
  case "$prev" in ''|*[!0-9]*) prev=0 ;; esac
  new=$(( total - prev )); [ "$new" -lt 0 ] && new=$total
  echo "$total" > "$REPL_OFFSET_FILE"
  # Published WITHOUT dimensions to match the dimensionless alarm in monitoring_s5.tf
  # (CloudWatch keys a metric by namespace+name+dimensions; the alarm has none).
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name replication_errors --unit Count --value "$new" 2>/dev/null || true
  [ "$new" -gt 0 ] && logger -t rebar-health "replication failures (new this interval)=${new}"
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
vtotal=$(journalctl CONTAINER_NAME="$VOTER_CONTAINER" --no-pager -o cat 2>/dev/null | grep -cE 'VOTER_ERROR') || true
vtotal=${vtotal:-0}
vprev=$(cat "$VOTER_OFFSET_FILE" 2>/dev/null || echo 0)
case "$vprev" in '' | *[!0-9]*) vprev=0 ;; esac
vnew=$((vtotal - vprev))
[ "$vnew" -lt 0 ] && vnew=$vtotal
echo "$vtotal" >"$VOTER_OFFSET_FILE"
# Published WITHOUT dimensions to match the dimensionless alarm in monitoring_s4b.tf
# (CloudWatch keys a metric by namespace+name+dimensions; the alarm has none).
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name voter_errors --unit Count --value "$vnew" 2>/dev/null || true
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
mtotal=$(journalctl CONTAINER_NAME="$VOTER_CONTAINER" --no-pager -o cat 2>/dev/null | grep -cE 'MERGE_CHANGE_ERROR') || true
mtotal=${mtotal:-0}
mprev=$(cat "$MERGE_OFFSET_FILE" 2>/dev/null || echo 0)
case "$mprev" in '' | *[!0-9]*) mprev=0 ;; esac
mnew=$((mtotal - mprev))
[ "$mnew" -lt 0 ] && mnew=$mtotal
echo "$mtotal" >"$MERGE_OFFSET_FILE"
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name review_bot_merge_change_errors --unit Count --value "$mnew" 2>/dev/null || true
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
dtotal=$(journalctl -u rebar-autodeploy.service --no-pager -o cat 2>/dev/null | grep -cE 'AUTODEPLOY_ERROR') || true
dtotal=${dtotal:-0}
dprev=$(cat "$DEPLOY_OFFSET_FILE" 2>/dev/null || echo 0)
case "$dprev" in '' | *[!0-9]*) dprev=0 ;; esac
dnew=$((dtotal - dprev))
[ "$dnew" -lt 0 ] && dnew=$dtotal
echo "$dtotal" >"$DEPLOY_OFFSET_FILE"
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name deploy_errors --unit Count --value "$dnew" 2>/dev/null || true
[ "$dnew" -gt 0 ] && logger -t rebar-health "auto-deploy failures (new this interval)=${dnew}"

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
gtotal=$(journalctl CONTAINER_NAME="$G2P_CONTAINER" --no-pager -o cat 2>/dev/null | grep -ciE "$G2P_PATTERN") || true
gtotal=${gtotal:-0}
gprev=$(cat "$G2P_OFFSET_FILE" 2>/dev/null || echo 0)
case "$gprev" in '' | *[!0-9]*) gprev=0 ;; esac
gnew=$((gtotal - gprev))
[ "$gnew" -lt 0 ] && gnew=$gtotal
echo "$gtotal" >"$G2P_OFFSET_FILE"
# Published WITHOUT dimensions to match the dimensionless alarm in monitoring_1fa8.tf
# (CloudWatch keys a metric by namespace+name+dimensions; the alarm has none).
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name g2p_dispatch_errors --unit Count --value "$gnew" 2>/dev/null || true
[ "$gnew" -gt 0 ] && logger -t rebar-health "g2p CI-dispatch failures (new this interval)=${gnew}"

# --- 5. Gerrit->GitHub mirror out-of-sync (WS7 / a774) ---------------------
# After the mirror-lock cutover, GitHub `main` only advances via Gerrit replication.
# If replication is stuck/failing, GitHub `main` falls BEHIND Gerrit `main` while Gerrit
# keeps moving — a silent drift the S5 error-count probe (section 3) does NOT catch (a
# push that never fires logs no failure). Publish mirror_out_of_sync = 1 when the two
# `main` SHAs differ, else 0. Both reads are ANONYMOUS (Gerrit public REST + a public
# `git ls-remote`), so no credentials are needed on the box. Transient lag (~15s after a
# submit) is absorbed by the alarm's multi-period evaluation window (monitoring_ws7.tf),
# not here. On a fetch failure we publish NOTHING (the alarm treats missing data as
# healthy) rather than risk a false alarm from a blip.
GERRIT_BASE_URL="${GERRIT_BASE_URL:-https://rebar.solutions.navateam.com}"
GITHUB_REPO_URL="${GITHUB_REPO_URL:-https://github.com/navapbc/rebar}"
gerrit_sha=$(curl -fsS --max-time 10 "${GERRIT_BASE_URL}/projects/rebar/branches/main" 2>/dev/null \
  | sed "s/)]}'//" | grep -oE '"revision": ?"[0-9a-f]+"' | grep -oE '[0-9a-f]{40}')
github_sha=$(git ls-remote "${GITHUB_REPO_URL}" refs/heads/main 2>/dev/null | awk '{print $1}')
if [ -n "$gerrit_sha" ] && [ -n "$github_sha" ]; then
  if [ "$gerrit_sha" = "$github_sha" ]; then oos=0; else oos=1; fi
  # Dimensionless to match the alarm in monitoring_ws7.tf.
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name mirror_out_of_sync --unit Count --value "$oos" 2>/dev/null || true
  [ "$oos" -gt 0 ] && logger -t rebar-health "mirror out-of-sync: gerrit=${gerrit_sha} github=${github_sha}"
else
  logger -t rebar-health "mirror sync check skipped (fetch failed: gerrit='${gerrit_sha}' github='${github_sha}')"
fi

# Always exit success on a completed probe run. Without this, the script's exit
# status is that of its last statement — and every metric section ends in a
# `[ "$n" -gt 0 ] && logger …` guard that is *false* on a healthy box (n=0),
# making the whole probe exit 1 and marking the systemd oneshot `failed` (which
# trips the deploy/health alarms). The probe reports state via metrics/journald,
# not its exit code; a run that reached here completed successfully.
exit 0
