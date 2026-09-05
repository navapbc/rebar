#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# container-cap.sh — the ONE place that says how big the WRITABLE CONTAINER LAYERS may get
# (ADR 0112 decisions 1+2, story 910b-2d43-4482-4c64).
#
# The 2026-09-02 outage filled the box's ROOT volume and took Gerrit, the review-bot's
# LLM-Review votes and the on-box MCP server down for ~5h. Writable container layers are the
# LAST of the four named root generators: they live INSIDE `/var/lib/docker/overlay2` as each
# container's `upperdir`, they grow independently of image layers and BuildKit cache, and
# nothing on this box measured them. The only signal was `rebar-root-disk-pressure` — "root
# disk high", which cannot name a generator.
#
# ## The ceiling that is NOT available here, and exactly why
#
# Docker's overlay2 driver DOES have a per-container size quota — `--storage-opt size=…`, or
# `storage-opts: ["overlay2.size=…"]` as a daemon default. It is accepted only when the
# filesystem backing `/var/lib/docker` is XFS mounted with the `pquota` option; otherwise the
# daemon refuses outright:
#
#     --storage-opt is supported only for overlay over xfs with 'pquota' mount option
#
# `/var/lib/docker` is on this box's ROOT filesystem, and XFS reads its quota mount options at
# MOUNT time and refuses to enable accounting on a remount. So enabling it needs
# `rootflags=pquota` on the kernel command line (GRUB_CMDLINE_LINUX), a grub regeneration and a
# REBOOT — a scheduled Gerrit outage, which a deploy tick may not take. That is the SAME
# constraint story 2ba3 hit for `/var/tmp`, re-derived for this mechanism and binding here too.
#
# Two further limits even after that reboot, so nobody plans around a stronger promise than the
# quota makes: it is PER-CONTAINER, not an aggregate ceiling over the share, and it is applied
# at container CREATION, so every live service must be recreated to acquire one.
#
# This script therefore does what 2ba3 did: ship the mitigation, PUBLISH which regime the box is
# in (`--check-quota` feeds `container_quota_enforceable`), and put the enablement steps in the
# runbook rather than letting a runbook assert a ceiling that does not exist.
#
# ## What the reaper does NOT guarantee — first, not in a footnote
#
#   1. IT CANNOT RECLAIM A RUNNING CONTAINER'S WRITABLE LAYER AT ALL. Only exited/dead
#      containers can be removed, so for the LIVE compose set this file delivers measurement and
#      an alarm, NOT a bound. The one mechanism that would bound them is the per-container quota
#      above, and it is reboot-gated. This is a sharper limitation than a fill rate and it is
#      stated first because an operator reading "bounded" must not read it as covering Gerrit's
#      own writable layer.
#   2. For the debris it DOES bound, the enforced bound between two runs is
#      `cap + fill_rate x interval`, not `cap`. At the 2 GiB share and the 300 s period below, a
#      sustained net fill above ~7.2 MB/s exceeds the share before the reaper next runs — and
#      this gp3 volume does 125 MB/s baseline, roughly 17x that. A single runaway writer defeats
#      it; its real job is bounding STEADY debris accumulation.
#   3. Nothing that exited inside CONTAINER_MIN_AGE_SECONDS is ever removed, so a burst of fresh
#      exits is unreclaimable BY DESIGN. When it cannot get under the share it SAYS SO; it never
#      exits quietly as though it had.
#
# ## Why this is not `docker container prune`
#
# The ticket names `docker container prune --filter until=…`, and it cannot express what this
# host needs: prune's filter set is `until` and `label`, with NO name filter. `autodeploy.sh`
# starts the mcp blue-green backends with a BARE `docker run` that compose never sees, and reaps
# them itself in `mcp_retire_sweep` under a guard this script cannot replicate — it reads the
# nginx `/mcp/` upstream include and REFUSES to reap an exited container that is still the live
# backend, because `--restart always` would otherwise have restored it. Reaping one anyway is
# bug 9ea3 exactly: a transient exit became a PERMANENT 502 on 2026-09-02. So candidates are
# enumerated and removed individually, and three overlapping protected sets are spared (see
# `classify` below).
#
# ## Usage
#
#   container-cap.sh --print-env      # the share + policy, for observability.sh
#   container-cap.sh --print-units    # the rendered reaper service+timer, no writes
#   container-cap.sh --check-active   # 1/0 — is the reaper timer in force?
#   container-cap.sh --check-quota    # 1/0 — is a HARD XFS project quota ENFORCED on docker root?
#   container-cap.sh --reap           # one bounded oldest-first pass (what the timer runs)
#   container-cap.sh --install        # write units, enable the timer, then OBSERVE
#
# Every `--print-*` and `--check-*` mode is SIDE-EFFECT-FREE (the journald-cap.sh / vartmp-cap.sh
# precedent), so rendering and both checks are testable without root, without systemd, without
# XFS and without a docker daemon.
# ---------------------------------------------------------------------------
set -uo pipefail

# --- The share -------------------------------------------------------------
# ONE budget with an internal split, never a second cap over the same bytes (ADR 0112, and
# docker-storage-cap.sh's header states it): writable layers live INSIDE `/var/lib/docker`, so
# the share is READ from docker-storage-cap.sh rather than re-spelled here as a literal that a
# later edit could drift out of agreement.
CONTAINER_CAP_DOCKER_CAP_SH="${CONTAINER_CAP_DOCKER_CAP_SH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-storage-cap.sh}"
eval "$(bash "$CONTAINER_CAP_DOCKER_CAP_SH" --print-env 2>/dev/null)" || true
#
# There is deliberately NO fallback literal here. A default spelled in two files is a ceiling
# that drifts, and a reaper running against a guessed share is worse than one that does not run:
# it would delete containers to satisfy a number nobody chose. When the share cannot be read this
# script REFUSES to reap and says so — autodeploy.sh's `prune_docker_caches` takes exactly this
# position on the BuildKit cap ("skipping the capped builder prune rather than guessing a
# ceiling"), and observability.sh then publishes no percentage rather than one about no quantity.
CONTAINER_WRITABLE_BYTES="${DOCKER_CONTAINER_WRITABLE_BYTES:-}"
DOCKER_ROOT="${DOCKER_ROOT:-/var/lib/docker}"

# The reaper's grace window. Nothing that exited more recently than this is ever removed,
# however full the share — a container that exited seconds ago is very likely one an operator is
# about to read `docker logs` from, and this is the snapshot janitor's grace-window reasoning
# applied to a different tree.
CONTAINER_MIN_AGE_SECONDS="${CONTAINER_MIN_AGE_SECONDS:-900}"

# --- The protected sets ----------------------------------------------------
# Never eviction candidates, for three INDEPENDENT reasons, so losing any one of them still
# leaves the live set spared:
#
#   1. CONTAINER_KEEP_LABELS — a container carrying any of these labels belongs to something
#      that owns its own lifecycle. `com.docker.compose.project` is on every compose service
#      (gerrit, review-bot, opcert, compose-mcp-1); an EXITED compose service is a CRASHED
#      service whose logs are the evidence, so removing it destroys the forensics of an incident
#      in progress. `rebar.service` is the STABLE service identity `mcp_run_new` stamps on the
#      bare-`docker run` blue-green containers precisely because compose never sees them.
#   2. CONTAINER_KEEP_NAME_RE — `mcp_managed`'s OWN regex from autodeploy.sh, so the two agree by
#      construction. autodeploy reaps this set itself under the live-upstream guard (bug 9ea3).
#   3. The daemon: candidates come from `--filter status=exited --filter status=dead`, which
#      never lists a running container, and removal is `docker rm` WITHOUT `-f`, which the daemon
#      refuses for a running container. Neither is shell logic that can be got wrong here.
CONTAINER_KEEP_LABELS="${CONTAINER_KEEP_LABELS:-com.docker.compose.project rebar.service}"
MCP_CONTAINER_PREFIX="${MCP_CONTAINER_PREFIX:-rebar-mcp}"
MCP_COMPOSE_CONTAINER="${MCP_COMPOSE_CONTAINER:-compose-mcp-1}"
CONTAINER_KEEP_NAME_RE="${CONTAINER_KEEP_NAME_RE:-^(${MCP_CONTAINER_PREFIX}|${MCP_COMPOSE_CONTAINER})}"

CONTAINER_UNIT_DIR="${CONTAINER_UNIT_DIR:-/etc/systemd/system}"
CONTAINER_INSTALLED_PATH="${CONTAINER_INSTALLED_PATH:-/usr/local/bin/rebar-container-cap.sh}"

#: The filesystem the overlay2 quota would live on. `/var/lib/docker` is a directory on root
#: here; a box that later gives Docker its own volume points this at that mount.
CONTAINER_QUOTA_FS="${CONTAINER_QUOTA_FS:-/}"

#: The reaper timer's period, and its start timeout. The bound NESTS below the period for the
#: reason install-observability.sh records (bug 1205): a `Type=oneshot` with no TimeoutStartSec
#: gets TimeoutStartUSec=INFINITY, and because OnUnitActiveSec is measured from the last
#: COMPLETED activation, one run that never finishes does not delay the timer — it DELETES the
#: next elapse. A reaper that latches off is a ceiling that silently stops existing.
CONTAINER_REAP_PERIOD_MIN="${CONTAINER_REAP_PERIOD_MIN:-5}"
CONTAINER_REAP_TIMEOUT_SEC="${CONTAINER_REAP_TIMEOUT_SEC:-240}"

#: Per-docker-invocation bound. A wedged daemon must not hold the pass open until the unit's own
#: timeout; this is autodeploy.sh's `timeout 120 docker …` convention.
CONTAINER_DOCKER_TIMEOUT="${CONTAINER_DOCKER_TIMEOUT:-60}"

REAPER_UNIT=rebar-container-reaper

die() { printf 'container-cap: %s\n' "$*" >&2; exit 1; }
warn() { printf 'container-cap: %s\n' "$*" >&2; }

# A share nothing can parse is a ceiling that does not exist. Refuse loudly rather than reaping
# against a number that is not one.
case "$CONTAINER_WRITABLE_BYTES" in
  '') CONTAINER_WRITABLE_BYTES="" ;;
  *[!0-9]*) die "CONTAINER_WRITABLE_BYTES must be an integer byte count" ;;
esac
case "$CONTAINER_MIN_AGE_SECONDS" in
  '' | *[!0-9]*) die "CONTAINER_MIN_AGE_SECONDS must be an integer number of seconds" ;;
esac

# Bounded docker. `timeout` is absent on some developer hosts, so its absence degrades to an
# unbounded call plus the unit's own TimeoutStartSec rather than exit 127 and no call at all.
_docker() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "$CONTAINER_DOCKER_TIMEOUT" docker "$@"
  else
    docker "$@"
  fi
}

# --- Rendering -------------------------------------------------------------
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

# What `--print-units` shows: both units, marked, so a reviewer reads one artefact.
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

# --- Is the REAPER in force? -----------------------------------------------
# Two independent things have to be true, and either can quietly stop being true: the unit files
# have to be the ones this script renders, and the timer has to be running. An installed unit
# with a dead timer is a reaper that never reaps — the state most likely to be mistaken for a
# working ceiling, and the one a "usage is nominal" reading cannot distinguish from health.
# FAILS CLOSED.
reaper_in_effect() {
  local service="${CONTAINER_UNIT_DIR}/${REAPER_UNIT}.service"
  local timer="${CONTAINER_UNIT_DIR}/${REAPER_UNIT}.timer"
  [ -f "$service" ] || return 1
  [ -f "$timer" ] || return 1
  # The timer is compared EXACTLY: it carries the period, and a stale period is a different
  # ceiling. The service is checked for the SHAPE that matters instead of byte equality, because
  # its ExecStart legitimately names either the installed copy or the checkout depending on
  # whether `install` succeeded — pinning one would report a working reaper as dead.
  [ "$(render_timer)" = "$(cat "$timer" 2>/dev/null)" ] || return 1
  grep -qE '^ExecStart=.*container-cap\.sh --reap$' "$service" 2>/dev/null || return 1
  grep -qE "^TimeoutStartSec=${CONTAINER_REAP_TIMEOUT_SEC}\$" "$service" 2>/dev/null || return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl is-active --quiet "${REAPER_UNIT}.timer" 2>/dev/null
}

# --- Is a HARD per-container quota possible? -------------------------------
# Accounting alone MEASURES and bounds nothing, so "Accounting: ON" is not a ceiling — reporting
# it as one would be exactly the paper bound this epic exists to remove. Only ENFORCEMENT counts,
# and enforcement on the filesystem backing DOCKER_ROOT is the precondition overlay2's
# `--storage-opt size=` refuses without. FAILS CLOSED: no xfs_quota, a non-XFS root, an
# unreadable state, or enforcement off all answer 0, because the cost of over-claiming here is a
# writable-layer footprint everybody believes is capped.
quota_enforced() {
  command -v xfs_quota >/dev/null 2>&1 || return 1
  xfs_quota -x -c "state -p" "$CONTAINER_QUOTA_FS" 2>/dev/null |
    grep -qiE '^[[:space:]]*Enforcement:[[:space:]]*ON'
}

# --- The census ------------------------------------------------------------
# ONE `docker inspect --size` over every container, which yields the whole writable footprint
# AND the candidate set from a single daemon walk. `--size` is what makes the daemon compute
# `SizeRw`, the writable layer — the same field `docker system df` sums into its Containers row,
# so the reaper and observability.sh §2i are reading the same quantity from the same daemon and
# cannot disagree about what "writable layer" means.
#
# `{{index .Config.Labels "k"}}` renders `<no value>` for an absent key on some engine versions
# and the empty string on others; both are normalised to empty below.
# The label columns are DERIVED from CONTAINER_KEEP_LABELS rather than spelled out here. A
# keep-list that the census does not actually read would be decorative — the variable would
# promise a protection the code never applies, which is worse than having no variable at all.
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

# Is $1 (container name) protected, either by name or by any of the label values in $2 (the
# remaining `|`-separated census columns, one per CONTAINER_KEEP_LABELS entry)? 0 = protected.
#
# `{{index .Config.Labels "k"}}` renders `<no value>` for an absent key on some engine versions
# and the empty string on others; both count as "not carrying that label".
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

# --- The reaper ------------------------------------------------------------
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

  # Five fixed columns then the label columns; `read` with six names leaves every remaining
  # column, separators included, in `labels` — which is exactly what `protected` splits.
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

  # Oldest-first: the container that finished longest ago is the one whose logs are least likely
  # to still be wanted.
  while read -r epoch size id name; do
    [ -n "$id" ] || continue
    [ "$total" -le "$target" ] && break
    # NEVER `-f`. The daemon refuses to remove a RUNNING container without it, so a candidate
    # that started running between the census and here is refused BY THE DAEMON rather than by
    # this loop's own bookkeeping — the same guarantee mcp_retire_image relies on for images.
    # No `-v`/`--volumes`: named and bind volumes carry the source-of-truth state.
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

# Epoch seconds for a Docker RFC3339 timestamp, or 0 when it cannot be read. python3 rather than
# `date -d`, whose flags differ between GNU and BSD (the docker-storage-cap.sh precedent). A
# never-started container carries the zero timestamp 0001-01-01T00:00:00Z, which parses to a
# negative epoch and would read as infinitely old; it is reported as 0 (age unknown) instead, and
# an unknown age is treated as reapable only by the sort, never by the grace check.
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

# Report, in the strongest terms the evidence supports, which regime the box is in.
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

# --- Argument handling -----------------------------------------------------
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
    # Consumed with `eval "$(… --print-env)"` by observability.sh, so the published
    # percent-of-share and the share the reaper holds are the same number by construction.
    # SINGLE-QUOTED values, not bare ones. This is consumed with `eval "$(… --print-env)"`, and
    # two of these are not words: the keep-list contains a space and the name pattern contains
    # `^(…|…)`, which an eval of a bare assignment parses as a subshell and dies on. Bare output
    # worked for docker-storage-cap.sh only because every value there is an integer.
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

# --- Install ---------------------------------------------------------------
# The reaper unit runs from a copy under ${CONTAINER_INSTALLED_PATH}, following
# install-observability.sh, so the unit does not depend on the checkout staying where it is. If
# that copy cannot be made the unit points at the script in place and says so — a reaper running
# from the checkout is worth having; a unit pointing at nothing is not.
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
