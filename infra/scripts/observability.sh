#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# observability.sh — the rebar box's host observability probe (S2 + S5 + S4b + S7).
#
# Run periodically by a systemd timer (install-observability.sh). Each run publishes
# CloudWatch metrics + journald log lines:
#   1. Health probe of Gerrit + the review-bot (/review/health) -> journald +
#      rebar/host:{gerrit_healthy,reviewbot_healthy} (S2).
#   2. Gerrit data-volume disk-used-percent -> rebar/host:disk_used_percent (S2 alarm).
#   3. Gerrit->GitHub replication failures (replication_log) -> rebar/host:replication_errors (S5 alarm).
#   4. review-bot voter failures (VOTER_ERROR in journald) -> rebar/host:voter_errors (S4b alarm).
#   5. Gate reachability -> Rebar/Gate:GerritReachable (1/0), watched by the S7 gate-down
#      alarm (treat_missing_data=breaching catches a dead host / stopped probe).
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
