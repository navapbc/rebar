#!/usr/bin/env bash
# Bound writable container layers within the Docker storage budget under ADR 0112.
#
# Writable layers share `/var/lib/docker` with images and build cache. Docker can enforce an
# overlay2 size per container only when the backing XFS filesystem mounts with `pquota` at boot.
# Enabling it on root requires `rootflags=pquota` and a reboot. Quotas apply at container
# creation and do not impose an aggregate cap, so existing services must be recreated.
#
# Until quota enforcement is available, the reaper measures one shared budget and removes
# eligible exited debris. It cannot reclaim running layers or protected recent exits. Between
# runs, reclaimable debris can reach `cap + fill_rate x interval`. If it cannot restore the
# budget, it reports the remaining running and protected bytes.
#
# `docker container prune` has no name filter. MCP blue-green containers require autodeploy's
# upstream-aware guard, so this script enumerates candidates. It independently protects compose
# and `rebar.service` labels, MCP names, and every running container.
#
# Usage:
#
#   container-cap.sh --print-env      # the share + policy, for observability.sh
#   container-cap.sh --print-units    # the rendered reaper service+timer, no writes
#   container-cap.sh --check-active   # 1/0 — is the reaper timer in force?
#   container-cap.sh --check-quota    # 1/0 — is a HARD XFS project quota ENFORCED on docker root?
#   container-cap.sh --reap           # one bounded oldest-first pass (what the timer runs)
#   container-cap.sh --install        # write units, enable the timer, then OBSERVE
#
# Every `--print-*` and `--check-*` mode is side-effect-free. They require no root access,
# systemd, XFS, or Docker daemon.
set -uo pipefail

# The share
# Writable layers reside inside `/var/lib/docker`. Read their share from docker-storage-cap.sh
# so ADR 0112 retains one budget rather than overlapping caps over the same bytes.
CONTAINER_CAP_DOCKER_CAP_SH="${CONTAINER_CAP_DOCKER_CAP_SH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-storage-cap.sh}"
eval "$(bash "$CONTAINER_CAP_DOCKER_CAP_SH" --print-env 2>/dev/null)" || true
#
# There is no numeric fallback. An unreadable share stops reaping rather than authorizing
# deletion against a guessed budget. Observability then omits the percentage.
CONTAINER_WRITABLE_BYTES="${DOCKER_CONTAINER_WRITABLE_BYTES:-}"
DOCKER_ROOT="${DOCKER_ROOT:-/var/lib/docker}"

# Preserve recent exits so operators can inspect their logs.
CONTAINER_MIN_AGE_SECONDS="${CONTAINER_MIN_AGE_SECONDS:-900}"

# The protected sets
# Three independent protections exclude candidates:
#
#   1. CONTAINER_KEEP_LABELS covers compose-owned services and `rebar.service` MCP instances.
#      Their lifecycle owners retain crash evidence and perform guarded cleanup.
#   2. CONTAINER_KEEP_NAME_RE matches autodeploy's MCP set, which uses the upstream-aware guard.
#   3. Candidate classification admits only exited or dead containers, and `docker rm` omits
#      `-f`. The daemon therefore refuses a container that starts running after the census.
CONTAINER_KEEP_LABELS="${CONTAINER_KEEP_LABELS:-com.docker.compose.project rebar.service}"
MCP_CONTAINER_PREFIX="${MCP_CONTAINER_PREFIX:-rebar-mcp}"
MCP_COMPOSE_CONTAINER="${MCP_COMPOSE_CONTAINER:-compose-mcp-1}"
CONTAINER_KEEP_NAME_RE="${CONTAINER_KEEP_NAME_RE:-^(${MCP_CONTAINER_PREFIX}|${MCP_COMPOSE_CONTAINER})}"

CONTAINER_UNIT_DIR="${CONTAINER_UNIT_DIR:-/etc/systemd/system}"
CONTAINER_INSTALLED_PATH="${CONTAINER_INSTALLED_PATH:-/usr/local/bin/rebar-container-cap.sh}"

#: Filesystem for the overlay2 quota. Point this at a dedicated Docker mount when present.
CONTAINER_QUOTA_FS="${CONTAINER_QUOTA_FS:-/}"

#: Reaper period and start bound. Keep the bound below the period. `OnUnitActiveSec` starts
#: after a completed activation, so an unbounded `Type=oneshot` run prevents later elapses.
CONTAINER_REAP_PERIOD_MIN="${CONTAINER_REAP_PERIOD_MIN:-5}"
CONTAINER_REAP_TIMEOUT_SEC="${CONTAINER_REAP_TIMEOUT_SEC:-240}"

#: Per-call Docker bound so one daemon request cannot consume the whole unit timeout.
CONTAINER_DOCKER_TIMEOUT="${CONTAINER_DOCKER_TIMEOUT:-60}"

REAPER_UNIT=rebar-container-reaper

die() { printf 'container-cap: %s\n' "$*" >&2; exit 1; }
warn() { printf 'container-cap: %s\n' "$*" >&2; }

# Reject malformed limits before they can authorize deletion.
case "$CONTAINER_WRITABLE_BYTES" in
  '') CONTAINER_WRITABLE_BYTES="" ;;
  *[!0-9]*) die "CONTAINER_WRITABLE_BYTES must be an integer byte count" ;;
esac
case "$CONTAINER_MIN_AGE_SECONDS" in
  '' | *[!0-9]*) die "CONTAINER_MIN_AGE_SECONDS must be an integer number of seconds" ;;
esac

# Bound Docker when `timeout` exists. Other hosts rely on the unit's `TimeoutStartSec` rather
# than failing with exit 127 before Docker runs.
_docker() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "$CONTAINER_DOCKER_TIMEOUT" docker "$@"
  else
    docker "$@"
  fi
}

render_service() {
  cat <<UNIT
[Unit]
Description=rebar exited-container reaper (bounded writable-layer budget)
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash ${EXEC_PATH} --reap
# Strictly below the timer period below, so a hung pass is killed BEFORE the next elapse would
# have been and can never overlap it. Without this a Type=oneshot gets an INFINITE start timeout,
# and one overrun deletes the next elapse rather than delaying it (bug 1205).
TimeoutStartSec=${CONTAINER_REAP_TIMEOUT_SEC}
# This walks the daemon's container set on a box whose job is serving Gerrit. Same pairing as
# rebar-observability.service and rebar-autodeploy.service.
Nice=10
IOSchedulingClass=idle
UNIT
}

render_timer() {
  cat <<UNIT
[Unit]
Description=Reap exited-container debris back under its byte share every ${CONTAINER_REAP_PERIOD_MIN} minutes

[Timer]
OnBootSec=${CONTAINER_REAP_PERIOD_MIN}min
OnUnitActiveSec=${CONTAINER_REAP_PERIOD_MIN}min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
}

# Mark both units in the side-effect-free `--print-units` output.
render_units() {
  printf '# ---- %s.service\n' "$REAPER_UNIT"
  render_service
  printf '# ---- %s.timer\n' "$REAPER_UNIT"
  render_timer
}

write_units() {
  local dir="$1"
  mkdir -p "$dir" || return 1
  render_service >"${dir}/${REAPER_UNIT}.service" || return 1
  render_timer >"${dir}/${REAPER_UNIT}.timer" || return 1
}

# Reaper enforcement
# Require current unit content and an active timer. Either missing condition fails closed.
reaper_in_effect() {
  local service="${CONTAINER_UNIT_DIR}/${REAPER_UNIT}.service"
  local timer="${CONTAINER_UNIT_DIR}/${REAPER_UNIT}.timer"
  [ -f "$service" ] || return 1
  [ -f "$timer" ] || return 1
  # Compare the timer exactly because its period defines the ceiling. Check only the service
  # contract because ExecStart may name the installed copy or the checkout fallback.
  [ "$(render_timer)" = "$(cat "$timer" 2>/dev/null)" ] || return 1
  grep -qE '^ExecStart=.*container-cap\.sh --reap$' "$service" 2>/dev/null || return 1
  grep -qE "^TimeoutStartSec=${CONTAINER_REAP_TIMEOUT_SEC}\$" "$service" 2>/dev/null || return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl is-active --quiet "${REAPER_UNIT}.timer" 2>/dev/null
}

# Quota enforcement
# Accounting does not bound storage. Report a hard ceiling only when XFS project-quota
# enforcement is active on the Docker filesystem. Missing tools or unreadable state fail closed.
quota_enforced() {
  command -v xfs_quota >/dev/null 2>&1 || return 1
  xfs_quota -x -c "state -p" "$CONTAINER_QUOTA_FS" 2>/dev/null |
    grep -qiE '^[[:space:]]*Enforcement:[[:space:]]*ON'
}

# Container census
# One `docker inspect --size` yields both the aggregate `SizeRw` footprint and candidates,
# matching observability.sh §2i. Label columns come from CONTAINER_KEEP_LABELS so every declared
# protection is inspected. Engines render absent labels as `<no value>` or empty; both work below.
census_format() {
  local fmt='{{.Id}}|{{.Name}}|{{.State.Status}}|{{.State.FinishedAt}}|{{.SizeRw}}' label
  for label in $CONTAINER_KEEP_LABELS; do
    fmt="${fmt}|{{index .Config.Labels \"${label}\"}}"
  done
  printf '%s\n' "$fmt"
}

census() {
  local ids
  ids="$(_docker ps -a --format '{{.ID}}' 2>/dev/null)" || return 1
  [ -n "$ids" ] || { printf '' ; return 0; }
  # shellcheck disable=SC2086
  _docker inspect --size --format "$(census_format)" $ids 2>/dev/null
}

# Return 0 when $1 matches a protected name or any `|`-separated label in $2 is present.
# Both `<no value>` and empty represent an absent label.
protected() {
  local name="$1" rest="${2:-}" label
  case "$name" in /*) name="${name#/}" ;; esac
  printf '%s' "$name" | grep -qE "$CONTAINER_KEEP_NAME_RE" && return 0
  while [ -n "$rest" ]; do
    label="${rest%%|*}"
    case "$rest" in *'|'*) rest="${rest#*|}" ;; *) rest="" ;; esac
    case "$label" in '' | '<no value>') continue ;; *) return 0 ;; esac
  done
  return 1
}

# Reaping
reap() {
  local rows total=0 target reclaimed=0 removed=0 protected_bytes=0 running_bytes=0
  local id name status finished size labels epoch now candidates

  if [ -z "$CONTAINER_WRITABLE_BYTES" ]; then
    warn "the writable-layer share is unreadable (docker-storage-cap.sh did not state \
DOCKER_CONTAINER_WRITABLE_BYTES); reaping NOTHING rather than deleting containers to satisfy a guessed ceiling"
    return 0
  fi

  rows="$(census)" || {
    warn "could not census containers (no docker daemon, or it did not answer within ${CONTAINER_DOCKER_TIMEOUT}s); reaping nothing"
    return 0
  }
  [ -n "$rows" ] || return 0

  target=$((CONTAINER_WRITABLE_BYTES * 80 / 100))
  now="$(date -u +%s)"
  candidates=""

  # Six `read` names leave all label columns, with separators, in `labels` for `protected`.
  while IFS='|' read -r id name status finished size labels; do
    [ -n "$id" ] || continue
    case "$size" in '' | *[!0-9]*) size=0 ;; esac
    total=$((total + size))
    if [ "$status" = "running" ]; then
      running_bytes=$((running_bytes + size))
      continue
    fi
    case "$status" in exited | dead) ;; *) protected_bytes=$((protected_bytes + size)); continue ;; esac
    if protected "$name" "$labels"; then
      protected_bytes=$((protected_bytes + size))
      continue
    fi
    epoch="$(finished_epoch "$finished")"
    if [ "$epoch" -gt 0 ] && [ $((now - epoch)) -lt "$CONTAINER_MIN_AGE_SECONDS" ]; then
      protected_bytes=$((protected_bytes + size))
      continue
    fi
    candidates="${candidates}${epoch} ${size} ${id} ${name}
"
  done <<EOF
$rows
EOF

  if [ "$total" -le "$CONTAINER_WRITABLE_BYTES" ]; then
    return 0
  fi

  # Remove oldest exits first because their logs are least likely to be needed.
  while read -r epoch size id name; do
    [ -n "$id" ] || continue
    [ "$total" -le "$target" ] && break
    # Omit `-f` so Docker rejects a candidate that restarted after the census. Omit
    # `-v`/`--volumes` because named and bind volumes may contain source-of-truth state.
    if _docker rm "$id" >/dev/null 2>&1; then
      total=$((total - size))
      reclaimed=$((reclaimed + size))
      removed=$((removed + 1))
      warn "reaped exited container ${name#/} (${id:0:12}, ${size}B)"
    else
      warn "could not remove ${name#/} (${id:0:12}); leaving it in place"
    fi
  done <<EOF
$(printf '%s' "$candidates" | sort -n)
EOF

  if [ "$total" -gt "$CONTAINER_WRITABLE_BYTES" ]; then
    warn "WARNING — writable container layers are still ${total}B against a ${CONTAINER_WRITABLE_BYTES}B share \
after removing ${removed} container(s) and reclaiming ${reclaimed}B; ${running_bytes}B belongs to RUNNING \
containers this reaper cannot touch at all and ${protected_bytes}B is protected or inside the \
${CONTAINER_MIN_AGE_SECONDS}s grace window. This is the reaper's limit, NOT a ceiling being enforced — only \
an overlay2 per-container quota bounds a running container, and that needs rootflags=pquota and a reboot. \
See infra/runbooks/review-bot-ops.md"
  fi
  return 0
}

# Convert Docker RFC3339 to epoch seconds with portable Python, returning 0 for unreadable or
# never-started timestamps. Age 0 sorts first but never satisfies the recent-exit grace check.
finished_epoch() {
  python3 - "$1" <<'EPOCH' 2>/dev/null || printf '0\n'
import datetime
import re
import sys

raw = sys.argv[1].strip()
# Docker renders RFC3339 with NANOsecond precision; datetime.fromisoformat accepts at most
# microseconds on the Pythons this box ships, so the fraction is truncated to 6 digits.
match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$", raw)
epoch = 0
if match:
    head, frac, zone = match.groups()
    zone = "+00:00" if zone in (None, "Z") else zone
    if len(zone) == 5:  # +0000 -> +00:00
        zone = f"{zone[:3]}:{zone[3:]}"
    try:
        stamp = datetime.datetime.fromisoformat(f"{head}.{(frac or '0')[:6]}{zone}")
        epoch = int(stamp.timestamp())
    except (ValueError, OverflowError, OSError):
        epoch = 0
# A container that never ran carries the zero timestamp 0001-01-01T00:00:00Z, whose epoch is
# hugely negative and would read as infinitely old. Reported as 0 = "age unknown", which the
# grace check below treats as NOT recently finished — correct, because such a container has no
# logs anybody is about to read.
print(max(epoch, 0))
EPOCH
}

# Report the quota capability and reaper state supported by current evidence.
report_state() {
  if quota_enforced; then
    warn "XFS project quota is ENFORCED on ${CONTAINER_QUOTA_FS}, so a per-container overlay2 size \
ceiling CAN be set for containers created from now on (see infra/runbooks/review-bot-ops.md)"
  else
    warn "NOTE — no per-container writable-layer CEILING is possible on this host: overlay2's \
--storage-opt size= requires XFS with the pquota mount option on the filesystem backing \
${DOCKER_ROOT}, and XFS reads quota options at MOUNT time. The ${CONTAINER_WRITABLE_BYTES}B share is \
held only by ${REAPER_UNIT}.timer, which can remove EXITED debris and cannot touch a running \
container's writable layer at all"
  fi
  if reaper_in_effect; then
    warn "${REAPER_UNIT}.timer is in force"
  else
    warn "WARNING — ${REAPER_UNIT}.timer is NOT in force (units missing/stale, or the timer is not \
running); NOTHING is bounding exited-container debris"
  fi
}

# Arguments
mode=""
while [ $# -gt 0 ]; do
  case "$1" in
    --print-env | --print-units | --check-active | --check-quota | --reap | --install) mode="$1" ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[ -n "$mode" ] || mode="--install"

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
EXEC_PATH="$SCRIPT_PATH"

case "$mode" in
  --print-env)
    # observability.sh evaluates this output to share the reaper's budget. Quote values because
    # the label list contains spaces and the name regex contains shell metacharacters.
    printf "CONTAINER_WRITABLE_BYTES='%s'\n" "$CONTAINER_WRITABLE_BYTES"
    printf "CONTAINER_MIN_AGE_SECONDS='%s'\n" "$CONTAINER_MIN_AGE_SECONDS"
    printf "CONTAINER_KEEP_LABELS='%s'\n" "$CONTAINER_KEEP_LABELS"
    printf "CONTAINER_KEEP_NAME_RE='%s'\n" "$CONTAINER_KEEP_NAME_RE"
    exit 0
    ;;
  --print-units) render_units; exit 0 ;;
  --check-active)
    if reaper_in_effect; then printf '1\n'; else printf '0\n'; fi
    exit 0
    ;;
  --check-quota)
    if quota_enforced; then printf '1\n'; else printf '0\n'; fi
    exit 0
    ;;
  --reap) reap; exit 0 ;;
esac

# Installation
# Prefer an installed copy so the unit does not depend on the checkout path. On copy failure,
# use this script in place and report the fallback.
if install -m 0755 "$SCRIPT_PATH" "$CONTAINER_INSTALLED_PATH" 2>/dev/null; then
  EXEC_PATH="$CONTAINER_INSTALLED_PATH"
else
  warn "could not copy this script to ${CONTAINER_INSTALLED_PATH}; ${REAPER_UNIT}.service will run it from ${SCRIPT_PATH}"
fi

write_units "$CONTAINER_UNIT_DIR" ||
  warn "could not write the ${REAPER_UNIT} units into ${CONTAINER_UNIT_DIR}"

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload 2>/dev/null ||
    warn "systemctl daemon-reload failed; the reaper units may not be visible until the next reload"
  systemctl enable --now "${REAPER_UNIT}.timer" 2>/dev/null ||
    warn "could not enable ${REAPER_UNIT}.timer; nothing is bounding exited-container debris"
fi

report_state
exit 0
