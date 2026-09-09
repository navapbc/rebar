#!/usr/bin/env bash
# Periodic host probe installed by install-observability.sh. It publishes through the EC2
# role; no static CloudWatch credentials are used.
#
# Metric catalog (rebar/host unless noted):
#   0  probe_ok, probe_elapsed_seconds, probe_truncated
#   1  gerrit_healthy, reviewbot_healthy; Rebar/Gate:GerritReachable
#   1b mcp_healthy (the public nginx-to-application serving path)
#   2  disk_used_percent; 2b root_disk_used_percent
#   2c data_disk_debris_bytes
#   2d mem_available_percent, mem_used_percent, mem_probe_ok,
#      container_memory_rss_bytes, container_stats_ok, container_stats_unparsed_rows
#   2e gate_scratch_mounted, gate_scratch_volume_in_service, disk_used_percent
#   2f docker_storage_bytes, docker_storage_used_percent, docker_buildkit_cache_bytes,
#      docker_buildkit_cache_used_percent, docker_unaccounted_bytes, docker_du_seconds,
#      docker_du_ok
#   2g journal_bytes, journal_used_percent, journal_cap_in_effect
#   2h var_tmp_bytes, var_tmp_used_percent, var_tmp_cleanup_active,
#      var_tmp_hard_quota_in_effect
#   2i container_writable_bytes, container_exited_bytes,
#      container_writable_used_percent, container_reaper_active, container_quota_enforceable
#   3  replication_errors
#   4  voter_errors; 4b g2p_dispatch_errors; 4c review_bot_merge_change_errors;
#      4d deploy_errors; 4e deploy_deferrals, review_interrupts and reason-specific deploy
#      markers; 4f mcp_retire_cap, mcp_mem_abort
#   5  mirror_out_of_sync
set -uo pipefail

DOMAIN="${DOMAIN:-rebar.solutions.navateam.com}"
DATA_MOUNT="${DATA_MOUNT:-/var/gerrit}"
# Keep this default aligned with Terraform's `gate_scratch_mount`; tests override it.
GATE_SCRATCH_MOUNT="${GATE_SCRATCH_MOUNT:-/var/lib/rebar/gate-scratch}"
GATE_SCRATCH_VOLUME_ID_FILE="${GATE_SCRATCH_VOLUME_ID_FILE:-/var/lib/rebar/gate-scratch-volume-id}"
NS="rebar/host"

# --- Whole-probe deadline ---------------------------------------------------
# `clamped` is the sole wall-clock-bound door. It gives each expensive probe the smaller of
# its local ceiling and the remaining whole-run budget, excluding a tail reserve for later
# metrics and CloudWatch publication. A timed-out measurement stays silent without starving
# later sections. A source-scan test rejects direct uses of the lower-level bound helper.
#
# mechanism-ok: env_var PROBE_DEADLINE_SEC — 9313-1fac-9f32-4b07: the whole-probe wall-clock
# budget, which must track TimeoutStartSec in install-observability.sh; overridable so the
# tests can drive the exhausted-budget path without waiting four minutes.
PROBE_DEADLINE_SEC="${PROBE_DEADLINE_SEC:-240}"
# mechanism-ok: env_var PROBE_TAIL_RESERVE_SEC — 9313-1fac-9f32-4b07: the share of that budget
# reserved for the sections after the expensive ones, so none can be starved by an overrun.
PROBE_TAIL_RESERVE_SEC="${PROBE_TAIL_RESERVE_SEC:-80}"
PROBE_STARTED_AT="$(date +%s)"

# Leave at least one second so an exhausted probe runs, fails fast, and reports non-measurement.
probe_budget_left() {
  local left
  left=$(( PROBE_DEADLINE_SEC - ( $(date +%s) - PROBE_STARTED_AT ) - PROBE_TAIL_RESERVE_SEC ))
  [ "$left" -lt 1 ] && left=1
  printf '%s\n' "$left"
}

# --- Truncation reporting ---------------------------------------------------
# The ExecStopPost `--report-exit` path runs before all normal work. It always records the
# systemd result in journald, reads the region cached by successful main runs, and uses one
# bounded IMDS fallback only on cold start. It never calls CloudWatch with an unknown region.
# A manual invocation defaults to an untruncated result rather than inventing a failure.
# mechanism-ok: env_var REGION_CACHE — 9313-1fac-9f32-4b07: the region the truncation hook reads
# so it does not depend on IMDS answering during the stall it is reporting.
REGION_CACHE="${REGION_CACHE:-/var/lib/rebar/probe-region}"
# mechanism-ok: env_var INSTANCE_ID_CACHE — a7bd-0cee-404f-4c06: lets the main probe reuse the
# last valid instance id when IMDS has a transient miss, so head-of-script CloudWatch publishes
# do not all run with an empty InstanceId.
REGION_CACHE_DIR="${REGION_CACHE%/*}"
[ "$REGION_CACHE_DIR" = "$REGION_CACHE" ] && REGION_CACHE_DIR="."
INSTANCE_ID_CACHE="${INSTANCE_ID_CACHE:-${REGION_CACHE_DIR}/probe-instance-id}"
# mechanism-ok: env_var DOCKER_DU_OVERLAY2_DEVCHECK_SKIP — 9313-1fac-9f32-4b07: lets the tests
# drive docker_du_census without a real filesystem behind $DOCKER_ROOT.
DOCKER_DU_OVERLAY2_DEVCHECK_SKIP="${DOCKER_DU_OVERLAY2_DEVCHECK_SKIP:-}"

cached_region() {
  local candidate
  candidate="$(head -n 1 "$REGION_CACHE" 2>/dev/null || true)"
  case "$candidate" in *[!a-z0-9-]* | '') return 1 ;; esac
  printf '%s\n' "$candidate"
}

cached_instance_id() {
  local candidate
  candidate="$(head -n 1 "$INSTANCE_ID_CACHE" 2>/dev/null || true)"
  case "$candidate" in i-*) printf '%s\n' "$candidate" ;;
  *) return 1 ;;
  esac
}

metric_name_from_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --metric-name)
        shift
        printf '%s\n' "${1:-unknown}"
        return 0
        ;;
    esac
    shift
  done
  printf 'unknown\n'
}

put_metric_data() {
  local metric
  metric="$(metric_name_from_args "$@")"
  if aws cloudwatch put-metric-data "$@" 2>/dev/null; then
    return 0
  fi
  logger -t rebar-health \
    "cloudwatch put-metric-data FAILED metric=${metric} region=${REGION:-unresolved}"
  return 1
}

if [ "${1:-}" = "--report-exit" ]; then
  probe_result="${SERVICE_RESULT:-success}"
  probe_truncated=1
  [ "$probe_result" = "success" ] && probe_truncated=0
  report_region="$(cached_region || true)"
  if [ -z "$report_region" ]; then
    # Cold start only: take one bounded IMDS shot, then give up.
    report_token="$(curl -s --max-time 3 -X PUT http://169.254.169.254/latest/api/token \
      -H 'X-aws-ec2-metadata-token-ttl-seconds: 120' 2>/dev/null || true)"
    report_region="$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/placement/region \
      -H "X-aws-ec2-metadata-token: $report_token" 2>/dev/null || true)"
    case "$report_region" in *[!a-z0-9-]* | '') report_region="" ;; esac
  fi
  # Record locally before the optional CloudWatch publication.
  logger -t rebar-health \
    "probe exit report: SERVICE_RESULT=${probe_result} EXIT_CODE=${EXIT_CODE:-none} EXIT_STATUS=${EXIT_STATUS:-none} probe_truncated=${probe_truncated} region=${report_region:-unresolved}"
  if [ -n "$report_region" ]; then
    aws cloudwatch put-metric-data --region "$report_region" --namespace "$NS" \
      --metric-name probe_truncated --unit Count --value "$probe_truncated" 2>/dev/null || true
  fi
  exit 0
fi

# Fetch bounded IMDSv2 identity before publishing metrics.
TOKEN=$(curl -s --max-time 5 -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 120' 2>/dev/null || true)
REGION=$(curl -s --max-time 5 http://169.254.169.254/latest/meta-data/placement/region \
  -H "X-aws-ec2-metadata-token: $TOKEN" 2>/dev/null || true)
IID=$(curl -s --max-time 5 http://169.254.169.254/latest/meta-data/instance-id \
  -H "X-aws-ec2-metadata-token: $TOKEN" 2>/dev/null || true)
case "$REGION" in *[!a-z0-9-]* | '') REGION="$(cached_region || true)" ;; esac
case "$IID" in i-*) : ;; *) IID="$(cached_instance_id || true)" ;; esac

# Cache only a valid region for ExecStopPost; cache failure must not stop the main probe.
case "$REGION" in
  '' | *[!a-z0-9-]*) : ;;
  *)
    mkdir -p "$(dirname "$REGION_CACHE")" 2>/dev/null || true
    printf '%s\n' "$REGION" >"$REGION_CACHE" 2>/dev/null || true
    ;;
esac
case "$IID" in
  i-*)
    mkdir -p "$(dirname "$INSTANCE_ID_CACHE")" 2>/dev/null || true
    printf '%s\n' "$IID" >"$INSTANCE_ID_CACHE" 2>/dev/null || true
    ;;
esac

# --- 1. Health probes ------------------------------------------------------
gerrit_code=$(curl -sS -o /dev/null -w '%{http_code}' "https://${DOMAIN}/config/server/version" --max-time 10 2>/dev/null || echo 000)
review_probe=$(curl -sS -w $'\n%{http_code}' "https://${DOMAIN}/review/health" --max-time 10 2>/dev/null || printf '\n000')
if [[ "$review_probe" == *$'\n'* ]]; then
  review_code="${review_probe##*$'\n'}"
  review_body="${review_probe%$'\n'*}"
else
  review_code="$review_probe"
  review_body=""
fi
logger -t rebar-health "gerrit=/config/server/version:${gerrit_code} review-bot=/review/health:${review_code}"

# Health heartbeats use 1 for healthy and 0 otherwise.
gerrit_ok=0; [ "$gerrit_code" = "200" ] && gerrit_ok=1
review_ok=0; [ "$review_code" = "200" ] && review_ok=1
if [ -n "$review_body" ]; then
  review_payload_ok="$(
    python3 -c '
import json
import sys

try:
    body = json.loads(sys.argv[1])
except Exception:
    print(0)
else:
    print(
        1
        if isinstance(body, dict)
        and body.get("status") == "ok"
        and body.get("gerrit_auth", "ok") == "ok"
        else 0
    )
    ' "$review_body" 2>/dev/null || echo 0
  )"
  [ "$review_payload_ok" = "1" ] || review_ok=0
fi
put_metric_data --region "$REGION" --namespace "$NS" \
  --metric-name gerrit_healthy --unit Count --value "$gerrit_ok" \
  --dimensions InstanceId="$IID" 2>/dev/null || true
put_metric_data --region "$REGION" --namespace "$NS" \
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
put_metric_data --region "$REGION" --namespace "Rebar/Gate" \
  --metric-name GerritReachable --unit Count --value "$gerrit_ok" 2>/dev/null || true

# --- 1b. MCP serving-path health -------------------------------------------
# Probe the public TLS/nginx/upstream/application path, not a loopback container. An
# unauthenticated /mcp request is healthy only when application auth returns 401; 2xx and all
# other codes are unhealthy. Publish a dimensionless 1/0 heartbeat on every tick.
mcp_code=$(curl -sS -o /dev/null -w '%{http_code}' "https://${DOMAIN}/mcp" --max-time 10 2>/dev/null || echo 000)
mcp_ok=0; [ "$mcp_code" = "401" ] && mcp_ok=1
logger -t rebar-health "mcp=/mcp:${mcp_code} mcp_healthy=${mcp_ok}"
put_metric_data --region "$REGION" --namespace "$NS" \
  --metric-name mcp_healthy --unit Count --value "$mcp_ok" 2>/dev/null || true
[ "$mcp_ok" -eq 0 ] && logger -t rebar-health \
  "mcp serving path UNHEALTHY: https://${DOMAIN}/mcp returned '${mcp_code}' (expected 401); check the live rebar-mcp container and the materialized /etc/nginx/mcp-upstream.conf"

# --- 2. Disk usage of the Gerrit data volume -------------------------------
used_pct=$(df --output=pcent "$DATA_MOUNT" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$used_pct" ]; then
  put_metric_data --region "$REGION" --namespace "$NS" \
    --metric-name disk_used_percent --unit Percent --value "$used_pct" \
    --dimensions InstanceId="$IID",mount="$DATA_MOUNT" 2>/dev/null || true
  logger -t rebar-health "disk ${DATA_MOUNT} used_percent=${used_pct}"
fi

# --- 2b. Root filesystem usage ---------------------------------------------
# This dimensionless gauge measures space only; it does not detect IOPS saturation.
root_pct=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$root_pct" ]; then
  put_metric_data --region "$REGION" --namespace "$NS" \
    --metric-name root_disk_used_percent --unit Percent --value "$root_pct" 2>/dev/null || true
  logger -t rebar-health "disk / used_percent=${root_pct}"
fi

# --- 2e. Review-gate scratch volume ----------------------------------------
# Publish mountedness every tick using the same on-volume proof marker as gate admission.
# This avoids treating an unmounted directory's root-filesystem `df` result as healthy scratch.
scratch_mounted=0
[ -f "$GATE_SCRATCH_MOUNT/.gate-scratch-mounted" ] && scratch_mounted=1
put_metric_data --region "$REGION" --namespace "$NS" \
  --metric-name gate_scratch_mounted --unit Count --value "$scratch_mounted" 2>/dev/null || true
logger -t rebar-health "gate scratch ${GATE_SCRATCH_MOUNT} mounted=${scratch_mounted}"
if [ "$scratch_mounted" -eq 0 ]; then
  logger -t rebar-health \
    "gate scratch volume ${GATE_SCRATCH_MOUNT} is NOT mounted — rebar gate admission refuses every plan-review and completion-verifier run rather than writing to the ROOT filesystem; see infra/runbooks/review-bot-ops.md"
fi

scratch_volume_id="${GATE_SCRATCH_VOLUME_ID:-$(head -n 1 "$GATE_SCRATCH_VOLUME_ID_FILE" 2>/dev/null || true)}"
scratch_volume_probe="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)/scripts/assert_volumes_in_service.py"
if [ ! -f "$scratch_volume_probe" ]; then
  scratch_volume_probe="/usr/local/bin/rebar-assert-volumes-in-service.py"
fi
if [ -n "$scratch_volume_id" ]; then
  scratch_volume_value="-1"
  if [ -f "$scratch_volume_probe" ]; then
    scratch_volume_value="$(
      python3 "$scratch_volume_probe" --metric-value --volume-id "$scratch_volume_id" \
        --mount "$GATE_SCRATCH_MOUNT" 2>/dev/null
    )" || scratch_volume_value="-1"
    case "$scratch_volume_value" in
      1) scratch_volume_value="1" ;;
      0) scratch_volume_value="0" ;;
      *) scratch_volume_value="-1" ;;
    esac
  fi
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name gate_scratch_volume_in_service --unit Count --value "$scratch_volume_value" \
    --dimensions InstanceId="$IID",VolumeId="$scratch_volume_id",mount="$GATE_SCRATCH_MOUNT" \
    2>/dev/null || true
  logger -t rebar-health \
    "gate scratch volume ${scratch_volume_id} at ${GATE_SCRATCH_MOUNT} in_service=${scratch_volume_value}"
fi

# Publish a mount-dimensioned `df` reading only after mountedness is proven; otherwise stay silent.
scratch_pct=""
if [ "$scratch_mounted" -eq 1 ]; then
  scratch_pct=$(df --output=pcent "$GATE_SCRATCH_MOUNT" 2>/dev/null | tail -1 | tr -dc '0-9')
fi
if [ -n "$scratch_pct" ]; then
  put_metric_data --region "$REGION" --namespace "$NS" \
    --metric-name disk_used_percent --unit Percent --value "$scratch_pct" \
    --dimensions InstanceId="$IID",mount="$GATE_SCRATCH_MOUNT" 2>/dev/null || true
  logger -t rebar-health "disk ${GATE_SCRATCH_MOUNT} used_percent=${scratch_pct}"
fi

# --- 2f. Docker storage generators -----------------------------------------
# Compare layout-independent filesystem truth for the whole Docker root with the whole daemon
# ledger. One inode-deduplicated `find -xdev` census yields allocated bytes, apparent bytes,
# and an overlay2 diagnostic subtotal. One `docker system df` ledger covers Images,
# Containers, Build Cache, and Local Volumes. Unaccounted bytes are apparent filesystem bytes
# minus that ledger, floored at zero. A split mount can only make this residue under-report.
# Each reading publishes only when its own measurement succeeds; docker_du_ok carries census
# failure separately, and an unavailable overlay2 subtotal affects only the log breadcrumb.
# Percent denominators come from docker-storage-cap.sh, which also renders the enforced policy.
DOCKER_CAP_SH="${DOCKER_CAP_SH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-storage-cap.sh}"
eval "$(bash "$DOCKER_CAP_SH" --print-env 2>/dev/null)" || true
DOCKER_ROOT="${DOCKER_ROOT:-/var/lib/docker}"
# Bound both Docker measurements through the whole-probe budget. A census that cannot finish
# within 60 seconds is intentionally silent; docker_du_seconds, docker_du_ok, and probe_ok
# distinguish that accepted degradation from a truncated run.
DOCKER_DU_TIMEOUT="${DOCKER_DU_TIMEOUT:-60}"
DOCKER_DF_TIMEOUT="${DOCKER_DF_TIMEOUT:-15}"

# --- Bounded journal counting ----------------------------------------------
# Each marker counter persists `<total> <cursor>` atomically and reads only entries after that
# cursor, so work scales with the interval rather than retained history. Cold starts and legacy
# bare totals seed from the tail and publish zero. Empty intervals keep both fields. A rotated
# cursor is reseeded only after a bounded tail read proves the journal is readable; unreadable
# intervals publish nothing. Cursor trailers are removed before matching. State advances only
# after CloudWatch accepts the delta, and every journal read is clamped to the whole-probe budget.
# mechanism-ok: env_var JOURNAL_SCAN_TIMEOUT — 1205-63b2-2c01-4e7f: the §2d wall-clock bound on
# every journald read, overridable only so the tests can drive the timeout path.
JOURNAL_SCAN_TIMEOUT="${JOURNAL_SCAN_TIMEOUT:-10}"

# The deployment host has coreutils `timeout`; portable test hosts may not. Without it, run the
# cursor-bounded command directly and preserve its non-zero "unmeasured interval" result.
if command -v timeout >/dev/null 2>&1; then
  bounded() { timeout "$@"; }  # composition-door: the only raw `timeout` in this script
else
  bounded() { shift; "$@"; }
fi

# Cap-compliance heartbeats publish on every tick: 1 means active, 0 means measured inactive,
# and -1 means the check was unavailable or unparseable. Absence remains the publisher-dead
# signal. Existing `< 1` alarms treat both inactive and unknown as breaching.
HEARTBEAT_UNKNOWN=-1

heartbeat_value() {
  case "$1" in
    1) printf '1\n' ;;
    0) printf '0\n' ;;
    *) printf '%s\n' "$HEARTBEAT_UNKNOWN" ;;
  esac
}

# Resolve a cap script from ordered candidates: prefer the checkout sibling, then its installed
# `rebar-` name. If none exists, return the last candidate so failures name an actionable path.
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

# Set JOURNAL_TAIL_CURSOR with an O(1) tail seek. Preserve journalctl's status so an empty,
# readable journal remains distinct from a failed read.
journal_tail_cursor() {
  local out rc
  out="$(clamped "$JOURNAL_SCAN_TIMEOUT" journalctl "$@" --no-pager -o cat -n 1 \
    --show-cursor 2>/dev/null)"
  rc=$?
  JOURNAL_TAIL_CURSOR="$(printf '%s\n' "$out" | sed -n 's/^-- cursor: //p' | tail -1)"
  return $rc
}

# Commit total and cursor atomically so interruption cannot desynchronise them.
journal_state_write() {
  local file="$1" total="$2" cursor="$3" tmp
  mkdir -p "$(dirname "$file")" 2>/dev/null || true
  tmp="${file}.tmp.$$"
  printf '%s %s\n' "$total" "$cursor" >"$tmp" 2>/dev/null || return 1
  mv -f "$tmp" "$file" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 1; }
}

# Split a journal stream into global count and cursor values, removing the cursor trailer before
# matching. Globals avoid losing one result through command-substitution subshell semantics.
journal_count_stream() {
  local stream="$1" flags="$2" pattern="$3" body
  JOURNAL_STREAM_CURSOR="$(printf '%s\n' "$stream" | sed -n 's/^-- cursor: //p' | tail -1)"
  body="$(printf '%s\n' "$stream" | grep -v '^-- cursor: ')" || true
  JOURNAL_STREAM_COUNT="$(printf '%s\n' "$body" | grep -c $flags -- "$pattern")" || true
  case "$JOURNAL_STREAM_COUNT" in '' | *[!0-9]*) JOURNAL_STREAM_COUNT=0 ;; esac
}

# journal_marker_delta <state_file> <grep_flags> <pattern> [journalctl selectors...]
# Sets the delta and next state after a measured interval. On failure, callers publish nothing;
# they persist the returned state only after CloudWatch succeeds so failed publishes are retried.
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
    # Cold starts and legacy bare totals seed from the tail and publish zero.
    journal_tail_cursor "$@" || return 1
    JOURNAL_NEXT_CURSOR="$JOURNAL_TAIL_CURSOR"
    return 0
  fi

  out="$(clamped "$JOURNAL_SCAN_TIMEOUT" journalctl "$@" --no-pager -o cat \
    --after-cursor "$cursor" --show-cursor 2>/dev/null)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # A bounded tail read distinguishes an unreadable journal from a rotated cursor. Reseed a
    # healthy journal, but never fall back to scanning retained history.
    journal_tail_cursor "$@" || return 1
    [ -n "$JOURNAL_TAIL_CURSOR" ] &&
      journal_state_write "$state_file" "$prev_total" "$JOURNAL_TAIL_CURSOR"
    return 1
  fi

  journal_count_stream "$out" "$flags" "$pattern"
  JOURNAL_DELTA="$JOURNAL_STREAM_COUNT"
  # An empty interval carries no trailer, so retain the previous cursor.
  JOURNAL_NEXT_CURSOR="${JOURNAL_STREAM_CURSOR:-$cursor}"
  JOURNAL_NEXT_TOTAL=$((prev_total + JOURNAL_DELTA))
  return 0
}

# Intentionally unclamped: these best-effort caps can be exceeded, so values over 100 expose a
# breached budget instead of disguising it as exactly full. CloudWatch `Percent` is only a unit
# label and accepts values above 100; companion byte gauges retain the breach's magnitude.
pct_of_cap() {
  printf '%s\n' "$(( $1 * 100 / $2 ))"
}

# One metadata traversal sets allocated Docker-root bytes, apparent Docker-root bytes, and the
# overlay2 allocated subtotal. It returns non-zero when no complete result can be parsed. The
# single `find -xdev -printf` census replaces duplicate full-tree `du` walks.
docker_du_census() {
  local root out parsed root_dev ov_dev
  root="${1%/}"
  DOCKER_DU_TOTAL=""
  DOCKER_DU_APPARENT=""
  DOCKER_DU_OVERLAY2=""
  out="$(clamped "$DOCKER_DU_TIMEOUT" find "$root" -xdev -printf '%D\t%i\t%s\t%b\t%p\n' 2>/dev/null)" || return 1
  # `%b` is allocated 512-byte blocks; `%s` is apparent size. Device+inode dedupes hardlinks.
  parsed="$(printf '%s\n' "$out" | awk -F '\t' -v root="$root" '
    NF >= 5 {
      seen_any = 1
      key = $1 ":" $2
      if (seen[key]++) next
      path = $5
      blocks = $4
      size = $3
      allocated = blocks * 512
      total += allocated
      apparent += size
      if (path == root "/overlay2" || index(path, root "/overlay2/") == 1) {
        overlay_seen = 1
        overlay += allocated
      }
    }
    END {
      if (!seen_any) exit 1
      printf "%d %d %s\n", total, apparent, (overlay_seen ? overlay : "-")
    }
  ')" || return 1
  # `-x` prunes child mounts but still emits their mount-point stubs. If overlay2 is on another
  # device, mark only its diagnostic subtotal unknown rather than publishing that stub as usage.
  if [ -n "$DOCKER_DU_OVERLAY2_DEVCHECK_SKIP" ]; then
    :
  elif [ -d "$root/overlay2" ]; then
    root_dev="$(stat -c '%d' "$root" 2>/dev/null || stat -f '%d' "$root" 2>/dev/null || true)"
    ov_dev="$(stat -c '%d' "$root/overlay2" 2>/dev/null || stat -f '%d' "$root/overlay2" 2>/dev/null || true)"
    if [ -n "$root_dev" ] && [ -n "$ov_dev" ] && [ "$root_dev" != "$ov_dev" ]; then
      parsed="${parsed% *} -"
    fi
  fi
  read -r DOCKER_DU_TOTAL DOCKER_DU_APPARENT DOCKER_DU_OVERLAY2 <<CENSUS
$parsed
CENSUS
  case "$DOCKER_DU_TOTAL" in ''|*[!0-9]*) DOCKER_DU_TOTAL=""; return 1 ;; esac
  case "$DOCKER_DU_APPARENT" in ''|*[!0-9]*) DOCKER_DU_APPARENT=""; return 1 ;; esac
  case "$DOCKER_DU_OVERLAY2" in *[!0-9]*) DOCKER_DU_OVERLAY2="" ;; esac
  return 0
}

# Return accounted total, build cache, containers, and reclaimable containers from one
# `docker system df`. Total exactly the four known rows: Images, Containers, Build Cache, and
# Local Volumes. Parse SI and binary units. Ignore future unknown row types, but fail the whole
# ledger when a known row's size is unparseable. Missing container fields remain -1 so §2i stays
# silent rather than inventing zero.
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
        # Strip the presentation percentage; absent or invalid reclaimable size remains -1.
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
docker_apparent_bytes=""
docker_overlay2_bytes=""
docker_du_ok=0
# Time the single filesystem census and publish its success independently.
docker_du_started_at="$(date +%s)"
if docker_du_census "$DOCKER_ROOT"; then
  docker_total_bytes="$DOCKER_DU_TOTAL"
  docker_apparent_bytes="$DOCKER_DU_APPARENT"
  docker_overlay2_bytes="$DOCKER_DU_OVERLAY2"
  docker_du_ok=1
fi
docker_du_seconds=$(( $(date +%s) - docker_du_started_at ))
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name docker_du_seconds --unit Seconds --value "$docker_du_seconds" 2>/dev/null || true
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name docker_du_ok --unit Count --value "$docker_du_ok" 2>/dev/null || true

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
  # Publish residue only when both the whole-root apparent census and whole ledger exist.
  # overlay2 is diagnostic only and never participates in the arithmetic.
  if [ -n "$docker_apparent_bytes" ]; then
    docker_unaccounted=$(( docker_apparent_bytes - docker_accounted_bytes ))
    # Shared layers can make the ledger exceed inode-deduplicated apparent bytes. Floor that
    # version-dependent overlap at zero instead of publishing a misleading negative value.
    [ "$docker_unaccounted" -lt 0 ] && docker_unaccounted=0
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name docker_unaccounted_bytes --unit Bytes --value "$docker_unaccounted" 2>/dev/null || true
    logger -t rebar-health \
      "docker root bytes=${docker_total_bytes} apparent=${docker_apparent_bytes} overlay2=${docker_overlay2_bytes:-unread} ledger=${docker_accounted_bytes} unaccounted=${docker_unaccounted} (bytes docker prune cannot reach)"
  fi
fi

# --- 2g. journald storage ---------------------------------------------------
# Publish the exact journal tree's bytes, its percent of SystemMaxUse, and whether the running
# daemon actually loaded that ceiling. journald reads configuration only at startup, so the
# dimensionless cap heartbeat is independent of both gauges. Each gauge publishes only when
# its own measurement succeeds. journald-cap.sh supplies the shared configured denominator.
JOURNALD_CAP_SH="${JOURNALD_CAP_SH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/journald-cap.sh}"
eval "$(bash "$JOURNALD_CAP_SH" --print-env 2>/dev/null)" || true
JOURNAL_DIR="${JOURNAL_DIR:-/var/log/journal}"
# A bounded failed `du` is an unmeasured gauge and remains silent.
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
  # Publish magnitude independently from denominator availability.
  if [ -n "${JOURNAL_MAX_USE_BYTES:-}" ] && [ "${JOURNAL_MAX_USE_BYTES:-0}" -gt 0 ]; then
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name journal_used_percent --unit Percent \
      --value "$(pct_of_cap "$journal_bytes" "$JOURNAL_MAX_USE_BYTES")" 2>/dev/null || true
  fi
fi

# --- 2h. /var/tmp storage --------------------------------------------------
# Publish bytes and percent of the vartmp-cap.sh budget independently from two heartbeats:
# whether cleanup is active and whether a hard XFS project quota is in force. The ordinary
# deployment uses a timer-driven mitigation; a hard ceiling requires pquota and a reboot.
# Measure exactly VAR_TMP_DIR, publish heartbeats every tick, and leave failed gauges silent.
VARTMP_CAP_SH="${VARTMP_CAP_SH:-$(resolve_cap_sh "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vartmp-cap.sh" "${VAR_TMP_INSTALLED_PATH:-/usr/local/bin/rebar-vartmp-cap.sh}")}"
eval "$(bash "$VARTMP_CAP_SH" --print-env 2>/dev/null)" || true
VAR_TMP_DIR="${VAR_TMP_DIR:-/var/tmp}"
# Bound the unknown-size scratch walk; a timeout leaves its gauges silent.
VAR_TMP_DU_TIMEOUT="${VAR_TMP_DU_TIMEOUT:-60}"

var_tmp_cleanup="$(heartbeat_value "$(bash "$VARTMP_CAP_SH" --check-active 2>/dev/null)")"
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name var_tmp_cleanup_active --unit Count --value "$var_tmp_cleanup" 2>/dev/null || true

var_tmp_quota="$(heartbeat_value "$(bash "$VARTMP_CAP_SH" --check-quota 2>/dev/null)")"
# This unalarmed capacity fact explains whether the percentage has a hard ceiling.
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
  # Publish magnitude independently from denominator availability.
  if [ -n "${VAR_TMP_MAX_BYTES:-}" ] && [ "${VAR_TMP_MAX_BYTES:-0}" -gt 0 ]; then
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name var_tmp_used_percent --unit Percent \
      --value "$(pct_of_cap "$var_tmp_bytes" "$VAR_TMP_MAX_BYTES")" 2>/dev/null || true
  fi
fi

# --- 2i. Writable container layers -----------------------------------------
# Publish total writable-layer bytes, reclaimable exited-container bytes, and percent of the
# shared container budget. The reaper heartbeat is alarmed; the unalarmed quota heartbeat says
# whether this host could enforce a hard overlay2 per-container ceiling. The reaper removes only
# EXITED containers, so running-layer usage is measured but not capped. Reuse the Containers row
# from §2f's single daemon ledger. Publish totals, percentages, reclaimable bytes, reaper state,
# and quota state independently; failed non-heartbeat measurements remain silent.
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
  # Publish magnitude independently from denominator availability.
  if [ -n "${CONTAINER_WRITABLE_BYTES:-}" ] && [ "${CONTAINER_WRITABLE_BYTES:-0}" -gt 0 ]; then
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
      --metric-name container_writable_used_percent --unit Percent \
      --value "$(pct_of_cap "$docker_container_bytes" "$CONTAINER_WRITABLE_BYTES")" 2>/dev/null || true
  fi
fi

# Publish reclaimable debris independently from total writable-layer bytes.
if [ -n "$docker_container_reclaimable" ]; then
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name container_exited_bytes --unit Bytes --value "$docker_container_reclaimable" 2>/dev/null || true
  logger -t rebar-health "exited-container debris bytes=${docker_container_reclaimable}"
fi

# --- 2c. Non-site debris on the Gerrit data volume -------------------------
# Treat every top-level entry except the Gerrit `site` tree and `lost+found` as debris. This
# detects misplaced operator evidence; its storage policy remains in the reclaim runbook.
# Publish a reading only when DATA_MOUNT is observable, because zero must mean a measured-clean
# volume. DATA_DEBRIS_ALLOW is a test seam, not a production tuning knob.
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
    # Use portable `du -sk`; clamp every unknown-size debris walk to the probe budget.
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

# --- 2d. Host memory and per-container RSS ---------------------------------
# These measurement-only metrics establish data for later limits. Host gauges are
# dimensionless heartbeats; a failed read publishes pessimistic 0%-available/100%-used with
# mem_probe_ok=0. Container RSS uses stable service and InstanceId dimensions.

# `free -k` available memory includes reclaimable cache; old procps falls back to free+cache.
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
  # Publish pessimistic values with an explicit failed-probe heartbeat.
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

# Bound both Docker calls, and make stats a single sample. Join stats to the `docker ps` label
# map: explicit `rebar.service` wins, Compose's service label is the fallback, and all missing
# labels share one `unlabeled` bucket so deploy-generated names cannot create unbounded metric
# cardinality. Parse binary and decimal memory units. Count and log every unparseable row while
# keeping census availability (`container_stats_ok`) separate from row quality; publish both
# heartbeats every tick.
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
    # Missing or non-numeric usage is a counted parse drop, never a zero-byte container.
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
# The census heartbeat distinguishes no rows from absent per-container datapoints.
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name container_stats_ok --unit Count --value "$container_stats_ok" 2>/dev/null || true
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name container_stats_unparsed_rows --unit Count --value "$container_unparsed" 2>/dev/null || true
[ "$container_stats_ok" -eq 0 ] && logger -t rebar-health \
  "container memory census produced no rows (docker stats failed, timed out, or no containers are running)"
# --- 3. Gerrit-to-GitHub replication failures ------------------------------
# Convert cumulative failure signatures in replication_log into a per-interval delta.
REPL_LOG="${REPL_LOG:-/var/gerrit/site/logs/replication_log}"
REPL_OFFSET_FILE="${REPL_OFFSET_FILE:-/var/lib/rebar/repl-fail-offset}"
if [ -f "$REPL_LOG" ]; then
  mkdir -p "$(dirname "$REPL_OFFSET_FILE")"
  # `grep -c` prints zero but exits 1 on no matches; capture its single line. These are free-form
  # replication-log phrases, not structured record prefixes, so do not anchor them.
  total=$(grep -cE 'REJECTED_NONFASTFORWARD|non-fast-forward|Giving up|giving up after|\[ERROR\]' "$REPL_LOG" 2>/dev/null) || true
  total=${total:-0}
  prev=$(cat "$REPL_OFFSET_FILE" 2>/dev/null || true)
  # Cold-start or corrupt offsets seed at the current total so retained history is not republished.
  case "$prev" in ''|*[!0-9]*) prev=$total ;; esac
  new=$(( total - prev ))
  # Rotation can make total smaller than the persisted offset. Publish zero rather than
  # double-counting retained entries, then persist the smaller baseline after publication.
  [ "$new" -lt 0 ] && new=0
  # The alarm expects this metric without dimensions.
  if aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name replication_errors --unit Count --value "$new" 2>/dev/null; then
    echo "$total" > "$REPL_OFFSET_FILE"
  fi
  [ "$new" -gt 0 ] && logger -t rebar-health "replication failures (new this interval)=${new}"
else
  # An absent log is not a failure event, so preserve the every-tick zero heartbeat. Mirror
  # divergence is measured independently in §5.
  aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
    --metric-name replication_errors --unit Count --value 0 2>/dev/null || true
  logger -t rebar-health "replication log ${REPL_LOG} absent; published replication_errors=0 heartbeat"
fi

# --- 4. Review-bot LLM-Review voter failures -------------------------------
# Count new structured VOTER_ERROR records from the host journal; the container needs no AWS
# credentials. Cursor state turns the stream into per-interval voter_errors.
VOTER_CONTAINER="${VOTER_CONTAINER:-compose-review-bot-1}"
VOTER_OFFSET_FILE="${VOTER_OFFSET_FILE:-/var/lib/rebar/voter-fail-offset}"
mkdir -p "$(dirname "$VOTER_OFFSET_FILE")"
# Anchor to the emitted `<TOKEN> {json}` record so review prose naming the token is never counted.
# The logger copy omits the token, preventing double counts. The alarm is dimensionless.
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

# --- 4c. Review-bot merge-change path failures -----------------------------
# Count structured MERGE_CHANGE_ERROR records separately from the encompassing voter failure.
MERGE_OFFSET_FILE="${MERGE_OFFSET_FILE:-/var/lib/rebar/merge-change-fail-offset}"
mkdir -p "$(dirname "$MERGE_OFFSET_FILE")"
# Record-anchor structured markers as in §4.
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

# --- 4d. Continuous auto-deploy failures -----------------------------------
# Count structured AUTODEPLOY_ERROR records from the systemd unit journal. Persistent values
# mean deployment is backing off while the last known-good release remains live.
DEPLOY_OFFSET_FILE="${DEPLOY_OFFSET_FILE:-/var/lib/rebar/autodeploy-fail-offset}"
mkdir -p "$(dirname "$DEPLOY_OFFSET_FILE")"
# Record anchoring also excludes tokens appearing in captured review-bot output.
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

# --- 4e. Review-drain and pressure outcomes --------------------------------
# Keep routine bounded deferrals distinct from alarm-worthy review interruptions. Publish both
# a rolled-up interrupt count and reason-specific counters: `bound-exceeded` means reviews
# remained busy through the drain budget; `signal-unavailable` means deployment ran without a
# readable in-flight signal. Use separate dimensionless metric names, not dimensions.
DEFER_OFFSET_FILE="${DEFER_OFFSET_FILE:-/var/lib/rebar/autodeploy-defer-offset}"
INTERRUPT_OFFSET_FILE="${INTERRUPT_OFFSET_FILE:-/var/lib/rebar/autodeploy-interrupt-offset}"
INTERRUPT_BOUND_OFFSET_FILE="${INTERRUPT_BOUND_OFFSET_FILE:-/var/lib/rebar/autodeploy-interrupt-bound-offset}"
INTERRUPT_SIGNAL_OFFSET_FILE="${INTERRUPT_SIGNAL_OFFSET_FILE:-/var/lib/rebar/autodeploy-interrupt-signal-offset}"
DISK_PRESSURE_OFFSET_FILE="${DISK_PRESSURE_OFFSET_FILE:-/var/lib/rebar/autodeploy-disk-pressure-offset}"
DISK_PRESSURE_PERSIST_OFFSET_FILE="${DISK_PRESSURE_PERSIST_OFFSET_FILE:-/var/lib/rebar/autodeploy-disk-pressure-persist-offset}"
# Keep MCP retire-cap and low-memory abort markers out of deploy_errors. They mean the
# blue-green port pool is still draining or the host cannot afford the two-container overlap.
MCP_RETIRE_CAP_OFFSET_FILE="${MCP_RETIRE_CAP_OFFSET_FILE:-/var/lib/rebar/autodeploy-mcp-retire-cap-offset}"
MCP_MEM_ABORT_OFFSET_FILE="${MCP_MEM_ABORT_OFFSET_FILE:-/var/lib/rebar/autodeploy-mcp-mem-abort-offset}"
mkdir -p "$(dirname "$DEFER_OFFSET_FILE")" "$(dirname "$INTERRUPT_OFFSET_FILE")" \
  "$(dirname "$INTERRUPT_BOUND_OFFSET_FILE")" "$(dirname "$INTERRUPT_SIGNAL_OFFSET_FILE")" \
  "$(dirname "$DISK_PRESSURE_OFFSET_FILE")" "$(dirname "$DISK_PRESSURE_PERSIST_OFFSET_FILE")" \
  "$(dirname "$MCP_RETIRE_CAP_OFFSET_FILE")" "$(dirname "$MCP_MEM_ABORT_OFFSET_FILE")"
# Each marker uses its own cursor, so calls scan intervals rather than retained histories.
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
# Record-anchor marker patterns at call sites; reason-specific counters extend the same ERE.
publish_autodeploy_marker_delta '^AUTODEPLOY_DEFERRED \{' deploy_deferrals \
  "$DEFER_OFFSET_FILE" "auto-deploys deferred for an in-flight review"
publish_autodeploy_marker_delta '^AUTODEPLOY_REVIEW_INTERRUPT \{' review_interrupts \
  "$INTERRUPT_OFFSET_FILE" "review-bot reviews interrupted by a deploy"
# Select reason-specific counters from the marker JSON; tolerate separator whitespace.
publish_autodeploy_marker_delta \
  '^AUTODEPLOY_REVIEW_INTERRUPT \{.*"reason":[[:space:]]*"bound-exceeded"' \
  review_interrupts_bound_exceeded "$INTERRUPT_BOUND_OFFSET_FILE" \
  "reviews interrupted after the deferral bound was exhausted (review-bot chronically busy)"
publish_autodeploy_marker_delta \
  '^AUTODEPLOY_REVIEW_INTERRUPT \{.*"reason":[[:space:]]*"signal-unavailable"' \
  review_interrupts_signal_unavailable "$INTERRUPT_SIGNAL_OFFSET_FILE" \
  "reviews interrupted with the in-flight signal UNREADABLE (deploys are running blind)"
# Count pressure-triggered reclaim attempts diagnostically; the root-pressure gauge owns alarming.
publish_autodeploy_marker_delta '^AUTODEPLOY_DISK_PRESSURE \{' disk_pressure_prunes \
  "$DISK_PRESSURE_OFFSET_FILE" "auto-deploy disk-pressure prunes"
# DISK_PRESSURE_PERSISTS records consecutive completed reclaim cycles that left pressure in
# place. Its distinct record-anchored token cannot cross-count ordinary pressure attempts.
publish_autodeploy_marker_delta '^AUTODEPLOY_DISK_PRESSURE_PERSISTS \{' disk_pressure_persists \
  "$DISK_PRESSURE_PERSIST_OFFSET_FILE" \
  "reclaim cycles that ran and left the disk STILL pressured (reclaim is ineffective)"
# Keep record-anchored MCP capacity outcomes separate from deploy errors.
publish_autodeploy_marker_delta '^AUTODEPLOY_MCP_RETIRE_CAP \{' mcp_retire_cap \
  "$MCP_RETIRE_CAP_OFFSET_FILE" "mcp blue-green retire/port-pool cap hits (releases not draining)"
publish_autodeploy_marker_delta '^AUTODEPLOY_MCP_MEM_ABORT \{' mcp_mem_abort \
  "$MCP_MEM_ABORT_OFFSET_FILE" "mcp blue-green deploys aborted for low memory on the 8GiB box"

# --- 4b. Gerrit-to-platform CI-dispatch failures ---------------------------
# Count new free-form g2p dispatch failures in the Gerrit container's host journal. This
# observes Gerrit-to-GitHub workflow dispatch; vote-back status lives in GitHub Actions.
# Publish a dimensionless per-interval delta without giving the container AWS credentials.
G2P_CONTAINER="${G2P_CONTAINER:-compose-gerrit-1}"
G2P_OFFSET_FILE="${G2P_OFFSET_FILE:-/var/lib/rebar/g2p-fail-offset}"
G2P_PATTERN="${G2P_PATTERN:-gerrit_to_platform.*(error|critical|traceback|exception)|failed to dispatch|workflow_dispatch.*(fail|error)|dispatch.*http (4|5)[0-9][0-9]}"
mkdir -p "$(dirname "$G2P_OFFSET_FILE")"
# Do not record-anchor these phrase/level matches. journal_marker_delta removes cursor metadata
# before matching, and the alarm expects no dimensions.
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

# --- 5. Gerrit-to-GitHub mirror comparison ---------------------------------
# Compare anonymous public main SHAs. Publish 0 only for a proven match; publish breaching value
# 1 for mismatch or either failed read. The alarm window absorbs ordinary replication lag and
# isolated fetch failures.
GERRIT_BASE_URL="${GERRIT_BASE_URL:-https://rebar.solutions.navateam.com}"
GITHUB_REPO_URL="${GITHUB_REPO_URL:-https://github.com/navapbc/rebar}"
gerrit_sha=$(curl -fsS --max-time 10 "${GERRIT_BASE_URL}/projects/rebar/branches/main" 2>/dev/null \
  | sed "s/)]}'//" | grep -oE '"revision": ?"[0-9a-f]+"' | grep -oE '[0-9a-f]{40}')
# Clamp the GitHub read; an empty result follows the failed-comparison path.
github_sha=$(clamped 15 git ls-remote "${GITHUB_REPO_URL}" refs/heads/main 2>/dev/null \
  | awk '{print $1}')
if [ -n "$gerrit_sha" ] && [ -n "$github_sha" ]; then
  if [ "$gerrit_sha" = "$github_sha" ]; then oos=0; else oos=1; fi
else
  # An unprovable match is breaching, not healthy or absent.
  oos=1
  logger -t rebar-health "mirror sync check failed, publishing mirror_out_of_sync=1 (gerrit='${gerrit_sha}' github='${github_sha}')"
fi
# Publish without dimensions on every path; absence therefore means the probe itself is dead.
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name mirror_out_of_sync --unit Count --value "$oos" 2>/dev/null || true
[ "$oos" -gt 0 ] && logger -t rebar-health "mirror out-of-sync: gerrit=${gerrit_sha} github=${github_sha}"

# --- Completion heartbeat --------------------------------------------------
# Emit probe_ok only after every section has run; elapsed time shows proximity to the deadline.
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name probe_elapsed_seconds --unit Seconds \
  --value "$(( $(date +%s) - PROBE_STARTED_AT ))" 2>/dev/null || true
aws cloudwatch put-metric-data --region "$REGION" --namespace "$NS" \
  --metric-name probe_ok --unit Count --value 1 2>/dev/null || true

# A run reaching this point reports health through metrics and exits successfully.
exit 0
