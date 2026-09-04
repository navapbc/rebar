#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# journald-cap.sh — the ONE place that says how big the persistent journal may get
# (ADR 0112 decisions 1+2, story e956-b1c3-45b9-4016).
#
# At the 2026-09-02 outage the box's ROOT volume filled and took Gerrit, the review-bot's
# LLM-Review votes and the on-box MCP server down for ~5h. `/var/log` was 1.8G of the 28G
# working set and 1.7G of that was the journal — every compose service logs to the host
# journal — while the only signal was `rebar-root-disk-pressure`: "root disk high", which
# cannot name which of the four accumulators grew.
#
# ## What this actually enforces, and how strongly
#
# `SystemMaxUse=` is a REAL cap enforced by the writer itself: journald checks it
# SYNCHRONOUSLY as it extends a journal file and vacuums to stay under it. That is materially
# stronger than the sibling Docker image/layer share (docker-storage-cap.sh), which has no
# ceiling at all and is held only by retention. Two residuals, stated rather than glossed:
#
#   1. ONLY ARCHIVED FILES ARE VACUUMED. The ACTIVE journal file is never deleted, so usage can
#      exceed the ceiling by up to one file. That overshoot is bounded by `SystemMaxFileSize`,
#      whose default is 1/8 of `SystemMaxUse`, so the honest worst case is 9/8 x the ceiling —
#      3.375 GiB for the 3 GiB default below, not 3 GiB exactly.
#   2. IT BOUNDS /var/log/journal, NOT /var/log. Anything else under /var/log is outside this
#      cap. At the incident that remainder was ~0.1G of the 1.8G — small, but unbounded here
#      and covered only by the rebar-root-disk-pressure backstop.
#
# ## journald is ALREADY capped — what this buys is not "a bound where there was none"
#
# Unset, `SystemMaxUse=` defaults to 10% of the filesystem CAPPED AT 4G, which on this 60 GiB
# root resolves to 4 GiB; the journal measured 1.7G, i.e. INSIDE its implicit ceiling. So this
# script does not convert an unbounded generator into a bounded one. It buys three things:
# an explicit, smaller ceiling that is PINNED REGARDLESS OF VOLUME SIZE (the derived default
# moves when the volume does); a ceiling that is READABLE ON THE BOX rather than existing only
# inside journald; and, with observability.sh 2g, a NAMED GENERATOR — the gap ADR 0112
# decision 2 exists to close.
#
# ## Two knobs are deliberately NOT set
#
#   * `SystemKeepFree=` — default 15% of the filesystem, also capped at 4G, so it is ALREADY at
#     its cap here. Pinning it changes nothing, and pinning a SMALLER value would WEAKEN the
#     free-space floor. Left derived on purpose.
#   * `RuntimeMaxUse=` — governs /run/log/journal, which is tmpfs: RAM, not the root volume.
#     That is the memory budget ADR 0079/0104 own, not this story's.
#
# ## Usage
#
#   journald-cap.sh --print-env      # ceiling + paths, for observability.sh
#   journald-cap.sh --print-conf     # the rendered drop-in, no writes
#   journald-cap.sh --check-active   # 1/0 — is the ceiling the LIVE journald is enforcing?
#   journald-cap.sh --install        # write it, restart journald, then OBSERVE the result
#
# `--print-env`, `--print-conf` and `--check-active` are SIDE-EFFECT-FREE (the
# compose-up.sh `--print-volumes` precedent), so rendering and the activation check are
# testable without root, without systemd and without journald.
# ---------------------------------------------------------------------------
set -uo pipefail

# --- The ceiling -----------------------------------------------------------
# MEASURED default, operator-settable (ADR 0112 decision 6 — a default is a starting point
# sized from one host's measurement, never a frozen constant): 3 GiB is 1.76x the 1.7G the
# journal actually held at the outage and 25% below the 4 GiB journald would derive, and unlike
# the derived value it does not move when the volume is resized.
JOURNAL_MAX_USE_BYTES="${JOURNAL_MAX_USE_BYTES:-3221225472}" # 3 GiB
JOURNAL_DIR="${JOURNAL_DIR:-/var/log/journal}"
# The `99-` prefix is load-bearing rather than decoration: systemd merges drop-ins in
# LEXICOGRAPHIC FILENAME ORDER and the last assignment wins, so sorting last makes "ours wins"
# a property of the name instead of an accident of what else the distro ships.
JOURNALD_DROPIN="${JOURNALD_DROPIN:-/etc/systemd/journald.conf.d/99-rebar-disk-ceiling.conf}"

#: The unit that reads it. Not a knob — journald is journald.
JOURNALD_UNIT=systemd-journald

#: Where a running process's start time is read from. A seam so the activation check below is
#: testable without a live journald; nothing in production overrides it.
JOURNALD_PROC_DIR="${JOURNALD_PROC_DIR:-/proc}"

die() { printf 'journald-cap: %s\n' "$*" >&2; exit 1; }
warn() { printf 'journald-cap: %s\n' "$*" >&2; }

# A ceiling journald cannot parse is a line it SILENTLY IGNORES — the config would look
# installed and no cap would exist. Refuse loudly instead. (journald reads a bare digit string
# as bytes, which is why the value is rendered without a unit suffix: exact, not
# unit-ambiguous.)
case "$JOURNAL_MAX_USE_BYTES" in
  ''|*[!0-9]*) die "JOURNAL_MAX_USE_BYTES must be an integer byte count" ;;
esac

# --- Rendering -------------------------------------------------------------
# A drop-in, not an edit of /etc/systemd/journald.conf: the distro owns that file, and a
# separate file is one this script can own end to end. That ownership is also why NO
# timestamped backup set is kept — the operator's undo is `rm` plus a restart, so this
# disk-ceiling script cannot grow an unbounded on-disk set of its own (the defect story 9183's
# review found in its daemon.json backups).
render_dropin() {
  cat <<CONF
# Managed by infra/scripts/journald-cap.sh (ADR 0112, story e956-b1c3-45b9-4016).
# Edits here are overwritten on the next deploy; change JOURNAL_MAX_USE_BYTES instead.
#
# SystemMaxUse is enforced by journald as it extends a journal file, but only ARCHIVED files
# are vacuumed, so real usage can exceed this by up to one active file (SystemMaxFileSize,
# default 1/8 of this value). It bounds ${JOURNAL_DIR} only, not the rest of /var/log.
[Journal]
SystemMaxUse=${JOURNAL_MAX_USE_BYTES}
CONF
}

# --- Is the installed ceiling actually in force? ----------------------------
# journald reads its configuration at STARTUP and nowhere else, and
# systemd-journald.service implements NO ExecReload — so `systemctl reload systemd-journald`
# cannot apply this and its exit status would prove nothing. Story 9183 shipped exactly that
# mistake against Docker (`systemctl reload docker` exits 0 while builder.gc is untouched) and
# reported "in effect" on the strength of it: ABSENCE OF EXECUTION REPORTED AS SUCCESS. A
# ceiling that reports itself installed while not in force is worse than no ceiling, because
# the loud half manufactures confidence.
#
# So nothing here infers activation from a command's exit status, INCLUDING the restart below.
# Activation is OBSERVED: the live journald is enforcing this file only if it started AFTER the
# file was last written.

# Epoch seconds of $1's mtime, or non-zero when it cannot be read. python3 rather than `stat`,
# whose format flags differ between GNU and BSD (the docker-storage-cap.sh precedent).
mtime_of() {
  python3 - "$1" <<'MTIME' 2>/dev/null
import os
import sys

try:
    print(int(os.stat(sys.argv[1]).st_mtime))
except OSError:
    raise SystemExit(1)
MTIME
}

# Epoch seconds at which the LIVE journald started, or non-zero when that cannot be
# established. `/proc/<pid>` is created by the kernel when the process is, so its mtime dates
# the running daemon — not the unit file, not the package, not the last restart attempt.
journald_started_at() {
  local pid
  pid="$(systemctl show "$JOURNALD_UNIT" --property=MainPID --value 2>/dev/null | tr -dc '0-9')"
  case "$pid" in ''|0) return 1 ;; esac
  mtime_of "${JOURNALD_PROC_DIR}/${pid}"
}

# True when the live journald demonstrably read $JOURNALD_DROPIN. FAILS CLOSED: an absent
# drop-in, an unreadable mtime, an unreadable PID or a daemon that predates the file all
# answer false, because the cost of over-claiming here is an unbounded journal.
cap_in_effect() {
  local started written
  command -v systemctl >/dev/null 2>&1 || return 1
  written="$(mtime_of "$JOURNALD_DROPIN")" || return 1
  started="$(journald_started_at)" || return 1
  [ "$started" -ge "$written" ]
}

# Report, in the strongest terms the evidence supports, whether the ceiling is in force.
report_activation_state() {
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl is-active --quiet "$JOURNALD_UNIT" 2>/dev/null; then
    # On a first boot compose-up.sh may install before the logger is up; the next start reads
    # the file for free. Neither a warning nor an in-force claim.
    warn "${JOURNALD_UNIT} is not running; it will read the ${JOURNAL_MAX_USE_BYTES}B journal ceiling when it next starts"
    return 0
  fi
  if cap_in_effect; then
    warn "the live ${JOURNALD_UNIT} started after this ceiling was written, so SystemMaxUse=${JOURNAL_MAX_USE_BYTES} IS in effect"
    return 0
  fi
  warn "WARNING — the ${JOURNAL_MAX_USE_BYTES}B journal ceiling is INSTALLED but is NOT in effect: the live ${JOURNALD_UNIT} predates it (or its start time could not be read). Restart it per infra/runbooks/review-bot-ops.md"
  return 0
}

# --- Applying it -----------------------------------------------------------
# Restarting journald is safe in a way restarting Docker is NOT, which is why story 9183
# refused a restart and this one takes it. Two mechanisms make it so, and both are properties
# of the SHIPPED unit rather than assumptions about it (systemd's units/systemd-journald.service.in):
#
#   Sockets=systemd-journald.socket systemd-journald-dev-log.socket
#       PID 1 owns the listening sockets, so datagrams queue rather than being refused while
#       journald is down.
#   FileDescriptorStoreMax=4224   (+ FileDescriptorStorePreserve=yes on newer systemd, whose
#       upstream comment is literally "Ensure services using StandardOutput=journal do not
#       break when journald is stopped")
#       journald hands its per-service STDOUT STREAM fds to PID 1's fd store on the way down
#       and reclaims them on the way up, so an already-running service keeps its log stream
#       across the restart instead of writing into a closed pipe.
#
# That second mechanism is THE RISKIEST ASSUMPTION in this script — if it did not hold, a
# restart would silently sever the log streams of every already-running service, which would in
# turn quietly zero the MARKER-COUNT metrics observability.sh derives from those journals
# (VOTER_ERROR, AUTODEPLOY_*, ...): healthy-looking readings from a box that stopped reporting.
# So it is CHECKED rather than believed: the fd store is probed on the live unit and the
# restart is REFUSED when it is absent or unreadable, leaving the ceiling dormant and saying so.
# A dormant ceiling is a capacity problem the alarms announce; a severed log stream is a blind
# spot that looks like health.
#
# The restart is still not EVIDENCE. Its exit status is logged as an action, never as
# activation; report_activation_state decides that independently, from the kernel.

# Size of the unit's file-descriptor store, or non-zero when it is absent/unreadable.
fd_store_max() {
  local value
  value="$(systemctl show "$JOURNALD_UNIT" --property=FileDescriptorStoreMax --value 2>/dev/null | tr -dc '0-9')"
  case "$value" in ''|0) return 1 ;; esac
  printf '%s\n' "$value"
}

restart_journald() {
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl is-active --quiet "$JOURNALD_UNIT" 2>/dev/null; then
    return 0
  fi
  if ! fd_store_max >/dev/null; then
    warn "WARNING — ${JOURNALD_UNIT} declares no file-descriptor store, so restarting it could sever the stdout log streams of already-running services; NOT restarting. The ceiling stays dormant until the next boot or an operator-scheduled restart (infra/runbooks/review-bot-ops.md)"
    return 0
  fi
  if systemctl restart "$JOURNALD_UNIT" 2>/dev/null; then
    warn "asked systemd to restart ${JOURNALD_UNIT} so it re-reads its configuration"
  else
    warn "WARNING — could not restart ${JOURNALD_UNIT}; the new ceiling stays dormant until it is restarted"
  fi
}

# --- Argument handling -----------------------------------------------------
mode=""
while [ $# -gt 0 ]; do
  case "$1" in
    --print-env|--print-conf|--check-active|--install) mode="$1" ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[ -n "$mode" ] || mode="--install"

case "$mode" in
  --print-env)
    # Consumed with `eval "$(… --print-env)"` by observability.sh, so the published
    # percent-of-cap and the ceiling journald is told to enforce are the same number by
    # construction and cannot drift apart.
    printf 'JOURNAL_MAX_USE_BYTES=%s\n' "$JOURNAL_MAX_USE_BYTES"
    printf 'JOURNAL_DIR=%s\n' "$JOURNAL_DIR"
    printf 'JOURNALD_DROPIN=%s\n' "$JOURNALD_DROPIN"
    exit 0
    ;;
  --print-conf)
    render_dropin
    exit 0
    ;;
  --check-active)
    if cap_in_effect; then printf '1\n'; else printf '0\n'; fi
    exit 0
    ;;
esac

# --- Install ---------------------------------------------------------------
rendered="$(render_dropin)" || die "could not render ${JOURNALD_DROPIN}"

# No-op fast path FIRST: compose-up.sh runs on every deploy tick, and re-running it must not
# bounce the logger to install bytes that are already there.
if [ -f "$JOURNALD_DROPIN" ] && [ "$rendered" = "$(cat "$JOURNALD_DROPIN" 2>/dev/null)" ]; then
  warn "${JOURNALD_DROPIN} already sets SystemMaxUse=${JOURNAL_MAX_USE_BYTES}; nothing to write"
  # "Nothing to write" is NOT "the ceiling is in force" — this is the one path on which it may
  # genuinely be, so it is the one path worth checking rather than assuming.
  report_activation_state
  exit 0
fi

dropin_dir="$(dirname "$JOURNALD_DROPIN")"
mkdir -p "$dropin_dir" || die "cannot create ${dropin_dir}"

# Bound the candidate's lifetime AT CREATION rather than on each exit path: an interrupt
# between the write and the `mv` would otherwise leave a half-considered config in systemd's
# own drop-in directory, where journald WOULD read it.
candidate="${JOURNALD_DROPIN}.candidate.$$"
trap 'rm -f "$candidate"' EXIT INT TERM
printf '%s\n' "$rendered" > "$candidate" || die "cannot write ${candidate}"
mv -f "$candidate" "$JOURNALD_DROPIN" || die "could not install ${JOURNALD_DROPIN}"
warn "installed the ${JOURNAL_MAX_USE_BYTES}B journal ceiling at ${JOURNALD_DROPIN}"

restart_journald
report_activation_state
exit 0
