#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# vartmp-cap.sh — the ONE place that says how big /var/tmp may get
# (ADR 0112 decisions 1+2, story 2ba3-bf77-1303-4b2d).
#
# At the 2026-09-02 outage the box's ROOT volume filled and took Gerrit, the review-bot's
# LLM-Review votes and the on-box MCP server down for ~5h. `/var/tmp` was 3.6G of the 28G
# working set, and the only signal was `rebar-root-disk-pressure`: "root disk high", which
# cannot name which of the four accumulators grew.
#
# ## The problem this file does NOT get to solve the easy way
#
# The sibling generators each had a native ceiling to switch on: journald checks `SystemMaxUse`
# synchronously as it extends a journal file (journald-cap.sh), and dockerd enforces
# `builder.gc` (docker-storage-cap.sh). `/var/tmp` has NO such writer. It is an ordinary
# directory on the root XFS filesystem written by anything on the box, and
# `systemd-tmpfiles` — which the approved design names — has **no size verb for an ordinary
# directory**. Its `q`/`Q` lines carry btrfs qgroup limits and do nothing on XFS. Age cleanup
# is all tmpfiles can give, and age is not a byte ceiling: a generator that writes 10 GiB in an
# hour is entirely unbounded by a 7-day age rule.
#
# ## The four candidate byte ceilings, and why this file lands where it does
#
#   1. XFS PROJECT QUOTA — a real ceiling. The kernel returns EDQUOT at the boundary; no fill
#      rate defeats it. CHOSEN as the ceiling, but it is OPERATOR-ENABLED: XFS reads its quota
#      mount options at MOUNT time and refuses to enable accounting on a remount, so on the
#      ROOT filesystem it requires `rootflags=pquota` on the kernel command line
#      (GRUB_CMDLINE_LINUX), a grub regeneration and a REBOOT. A reboot of this host is a
#      scheduled Gerrit outage; a deploy tick may not take one. So this script APPLIES the
#      quota when the kernel is already accounting, and otherwise says exactly what to do.
#      (AL2023 formats root XFS with the v5 superblock, so project and group quota can coexist;
#      on the pre-v5 format they share one on-disk field and are mutually exclusive. This script
#      does not assume either — it reads the live state.)
#   2. AGE CLEANUP via systemd-tmpfiles — real, installable now, bounds AGE only. Shipped as the
#      first line of defence, never described as a ceiling.
#   3. A BOUNDED OLDEST-FIRST REAPER on a timer — shipped, and it is what actually holds the
#      line until (1) is enabled. It is a MITIGATION WITH A FILL-RATE ASSUMPTION, quantified
#      below, not a ceiling.
#   4. Rejected: a loop-mounted fixed-size image at /var/tmp. It is a genuine ceiling with no
#      reboot, but it permanently consumes its full size from root whether used or not,
#      mounting over a live /var/tmp hides files that running processes hold open, and a sparse
#      image on a full root turns an ordinary /var/tmp ENOSPC into a shutdown of the image
#      filesystem. Worse failure modes than the problem, and unverifiable without applying it to
#      the host.
#   5. Rejected: bind /var/tmp onto the gate-scratch volume (story aa40). That MOVES the bytes
#      rather than bounding them, puts unrelated tmp traffic on a volume sized for gate scratch,
#      and — while that volume is attached but not mounted (bug dcc3-75ee-26ce-4840) — would
#      fail open onto root, which is precisely the "bounded on paper, unbounded in practice"
#      shape this epic exists to remove.
#
# ## What the reaper does NOT guarantee — stated, not glossed
#
# The reaper runs on a 5-minute timer. BETWEEN two runs /var/tmp is bounded only by the volume,
# so the enforced bound is `cap + fill_rate x interval`, not `cap`. At the 4 GiB default and a
# 300 s period, a sustained NET fill rate above ~14.6 MB/s can exceed the cap before the reaper
# next runs — and this volume is gp3 at 125 MB/s baseline throughput, roughly 8.5x that. So a
# single runaway writer defeats it; the reaper's real job is bounding STEADY accumulation, which
# is the shape /var/tmp actually had at the outage (3.6G of accreted job scratch, not one burst).
#
# Second assumption: the reaper only reclaims what it may delete. Nothing younger than
# VAR_TMP_MIN_AGE_SECONDS is ever evicted — the snapshot janitor's grace window, for the same
# reason (a directory written seconds ago is almost certainly still being written INTO) — so a
# burst of fresh files is unreclaimable BY DESIGN. When it cannot get under the ceiling it says
# so; it never exits quietly as though it had.
#
# The ONE mechanism with neither assumption is the project quota. That is why the box publishes
# `var_tmp_hard_quota_in_effect` and this script reports which regime it is in, rather than
# letting a runbook assert one.
#
# ## Usage
#
#   vartmp-cap.sh --print-env      # ceiling + paths, for observability.sh
#   vartmp-cap.sh --print-conf     # the rendered tmpfiles.d drop-in, no writes
#   vartmp-cap.sh --print-units    # the rendered reaper service+timer, no writes
#   vartmp-cap.sh --check-active   # 1/0 — are the age cleanup AND the reaper timer in force?
#   vartmp-cap.sh --check-quota    # 1/0 — is a HARD XFS project quota being ENFORCED?
#   vartmp-cap.sh --reap           # one bounded oldest-first eviction pass (what the timer runs)
#   vartmp-cap.sh --install        # write config + units, enable the timer, then OBSERVE
#
# Every `--print-*` and `--check-*` mode is SIDE-EFFECT-FREE (the journald-cap.sh precedent), so
# rendering and both activation checks are testable without root, without systemd and without
# XFS.
# ---------------------------------------------------------------------------
set -uo pipefail

# --- The ceiling -----------------------------------------------------------
# MEASURED default, operator-settable (ADR 0112 decision 6 — a default is a starting point sized
# from one host's measurement, never a frozen constant). 4 GiB sits deliberately either side of
# the 3.6 GiB /var/tmp actually reached on 2026-09-02: it is ABOVE it, so ordinary steady state
# does not thrash the reaper, and its 85% alarm threshold (3.4 GiB) is BELOW it, so this
# configuration would have NAMED /var/tmp before it reached its incident size. On the 60 GiB
# root that is 6.7% of the volume.
VAR_TMP_MAX_BYTES="${VAR_TMP_MAX_BYTES:-4294967296}" # 4 GiB
VAR_TMP_DIR="${VAR_TMP_DIR:-/var/tmp}"

# Age cleanup. AL2023 ships `q /var/tmp 1777 root root 30d` in /usr/lib/tmpfiles.d/tmp.conf;
# 7 days is a tightening of that, not a new bound where there was none. It is long enough that
# an investigation started on a Friday still has its evidence on Monday.
VAR_TMP_MAX_AGE_DAYS="${VAR_TMP_MAX_AGE_DAYS:-7}"

# The reaper's grace window. Nothing younger than this is ever evicted, however full the tree.
VAR_TMP_MIN_AGE_SECONDS="${VAR_TMP_MIN_AGE_SECONDS:-900}"

# Directories whose CHILDREN are the eviction candidates rather than the directory itself.
# `rebar-evidence` is the runbook's sanctioned investigation scratch location
# (/var/tmp/rebar-evidence/<ticket>-<stamp>/, task 3e92), and the epic flagged the collision
# explicitly: treating it as ONE candidate would either evict every investigation's evidence
# together or, once any child is fresh, protect all of it forever. Descending makes one stale
# investigation reclaimable while an active one is untouched.
VAR_TMP_DESCEND="${VAR_TMP_DESCEND:-rebar-evidence}"

# Never eviction candidates. `lost+found` is the filesystem's, and removing it breaks xfs_repair.
VAR_TMP_KEEP="${VAR_TMP_KEEP:-lost+found}"

# The `99-` prefix is load-bearing rather than decoration: systemd-tmpfiles merges configuration
# in LEXICOGRAPHIC FILENAME ORDER and a later file's line for the same path replaces an earlier
# one, so sorting after the distro's `tmp.conf` makes "ours wins" a property of the name instead
# of an accident of what else is installed.
VAR_TMP_TMPFILES_CONF="${VAR_TMP_TMPFILES_CONF:-/etc/tmpfiles.d/99-rebar-var-tmp.conf}"
VAR_TMP_UNIT_DIR="${VAR_TMP_UNIT_DIR:-/etc/systemd/system}"
VAR_TMP_INSTALLED_PATH="${VAR_TMP_INSTALLED_PATH:-/usr/local/bin/rebar-vartmp-cap.sh}"

#: The XFS project id the quota is keyed on. Not a knob anyone should need to turn; it exists so
#: an operator whose box already uses project ids can avoid a collision.
VAR_TMP_PROJECT_ID="${VAR_TMP_PROJECT_ID:-7312}"

#: The filesystem the quota lives on. /var/tmp is a directory on root here; a box that later
#: gives it its own volume points this at that mount.
VAR_TMP_QUOTA_FS="${VAR_TMP_QUOTA_FS:-/}"

#: The reaper timer's period, and its start timeout. The bound NESTS below the period for the
#: reason install-observability.sh records (bug 1205): a `Type=oneshot` with no
#: TimeoutStartSec gets TimeoutStartUSec=INFINITY, and because OnUnitActiveSec is measured from
#: the last COMPLETED activation, one run that never finishes does not delay the timer — it
#: DELETES the next elapse. A reaper that latches off is a ceiling that silently stops existing.
VAR_TMP_REAP_PERIOD_MIN="${VAR_TMP_REAP_PERIOD_MIN:-5}"
VAR_TMP_REAP_TIMEOUT_SEC="${VAR_TMP_REAP_TIMEOUT_SEC:-240}"

REAPER_UNIT=rebar-var-tmp-reaper

die() { printf 'vartmp-cap: %s\n' "$*" >&2; exit 1; }
warn() { printf 'vartmp-cap: %s\n' "$*" >&2; }

# A ceiling nothing can parse is a ceiling that does not exist. Refuse loudly rather than
# rendering config that looks installed and bounds nothing.
case "$VAR_TMP_MAX_BYTES" in
  '' | *[!0-9]*) die "VAR_TMP_MAX_BYTES must be an integer byte count" ;;
esac
case "$VAR_TMP_MIN_AGE_SECONDS" in
  '' | *[!0-9]*) die "VAR_TMP_MIN_AGE_SECONDS must be an integer number of seconds" ;;
esac

# --- Rendering -------------------------------------------------------------
# A drop-in, not an edit of the distro's tmpfiles.d: that file is the distro's, and a separate
# file is one this script can own end to end. That ownership is also why NO timestamped backup
# set is kept — the operator's undo is `rm` plus a `systemd-tmpfiles --create`, so this
# disk-ceiling script cannot grow an unbounded on-disk set of its own (the defect story 9183's
# review found in its daemon.json backups).
render_tmpfiles_conf() {
  cat <<CONF
# Managed by infra/scripts/vartmp-cap.sh (ADR 0112, story 2ba3-bf77-1303-4b2d).
# Edits here are overwritten on the next deploy; change VAR_TMP_MAX_AGE_DAYS instead.
#
# THIS BOUNDS AGE, NOT BYTES. systemd-tmpfiles has no size verb for a directory on an ordinary
# filesystem (its q/Q lines set btrfs qgroup limits and do nothing on XFS), so nothing on this
# line stops a generator filling the volume inside the age window. The byte ceiling is an XFS
# project quota, which needs rootflags=pquota and a reboot; until it is enabled the byte budget
# is held by ${REAPER_UNIT}.timer, a mitigation with a fill-rate assumption. See
# infra/runbooks/review-bot-ops.md.
d ${VAR_TMP_DIR} 1777 root root ${VAR_TMP_MAX_AGE_DAYS}d
CONF
}

render_units() {
  cat <<UNIT
# ---- ${REAPER_UNIT}.service
[Unit]
Description=rebar /var/tmp oldest-first reaper (bounded byte budget)

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash ${EXEC_PATH} --reap
# Strictly below the timer period below, so a hung pass is killed BEFORE the next elapse would
# have been and can never overlap it. Without this a Type=oneshot gets an INFINITE start
# timeout, and one overrun deletes the next elapse rather than delaying it (bug 1205).
TimeoutStartSec=${VAR_TMP_REAP_TIMEOUT_SEC}
# This walks a scratch tree on a box whose job is serving Gerrit. Same pairing as
# rebar-observability.service and rebar-autodeploy.service.
Nice=10
IOSchedulingClass=idle

# ---- ${REAPER_UNIT}.timer
[Unit]
Description=Reap /var/tmp back under its byte budget every ${VAR_TMP_REAP_PERIOD_MIN} minutes

[Timer]
OnBootSec=${VAR_TMP_REAP_PERIOD_MIN}min
OnUnitActiveSec=${VAR_TMP_REAP_PERIOD_MIN}min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
}

# The two units are rendered from one function so they cannot drift, and split on the marker
# lines when written.
write_units() {
  local dir="$1" all
  all="$(render_units)" || return 1
  mkdir -p "$dir" || return 1
  printf '%s\n' "$all" | awk -v dir="$dir" '
    /^# ---- / { file = dir "/" $3; next }
    file { print > file }
  '
}

# --- Is the CLEANUP in force? ----------------------------------------------
# Two independent things have to be true, and either can quietly stop being true: the drop-in
# has to be the one this script renders, and the reaper timer has to be running. An installed
# drop-in with a dead timer is age cleanup that never happens and a reaper that never reaps —
# the state most likely to be mistaken for a working ceiling. FAILS CLOSED.
cleanup_in_effect() {
  local rendered
  rendered="$(render_tmpfiles_conf)" || return 1
  [ -f "$VAR_TMP_TMPFILES_CONF" ] || return 1
  [ "$rendered" = "$(cat "$VAR_TMP_TMPFILES_CONF" 2>/dev/null)" ] || return 1
  [ -f "${VAR_TMP_UNIT_DIR}/${REAPER_UNIT}.timer" ] || return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl is-active --quiet "${REAPER_UNIT}.timer" 2>/dev/null
}

# --- Is a HARD QUOTA in force? ---------------------------------------------
# Accounting alone MEASURES the tree and bounds nothing, so "Accounting: ON" is not a ceiling —
# reporting it as one would be exactly the paper bound this epic exists to remove. Only
# enforcement counts. FAILS CLOSED: no xfs_quota, an unreadable state, or enforcement off all
# answer 0, because the cost of over-claiming here is an unbounded /var/tmp everybody believes
# is capped.
quota_state_line() {
  command -v xfs_quota >/dev/null 2>&1 || return 1
  xfs_quota -x -c "state -p" "$VAR_TMP_QUOTA_FS" 2>/dev/null
}

quota_accounting_on() {
  quota_state_line | grep -qiE '^[[:space:]]*Accounting:[[:space:]]*ON'
}

quota_enforced() {
  quota_state_line | grep -qiE '^[[:space:]]*Enforcement:[[:space:]]*ON'
}

# --- The reaper ------------------------------------------------------------
# One bounded python3 pass rather than a find|sort|xargs pipeline: it needs sizes and mtimes
# together, `stat`'s format flags differ between GNU and BSD (the docker-storage-cap.sh
# precedent), and a single process is one thing to bound rather than four.
#
# It sums APPARENT file sizes, while observability.sh sizes the same tree with `du -sx`, which
# counts allocated BLOCKS. The two therefore disagree slightly, and always in the same
# direction: blocks >= apparent size, so the reaper's view is the SMALLER one and it reaps
# marginally LATE rather than deleting bytes the metric had not yet counted. Late is the safe
# error for a destructive operation.
reap() {
  python3 - "$VAR_TMP_DIR" "$VAR_TMP_MAX_BYTES" "$VAR_TMP_MIN_AGE_SECONDS" \
    "$VAR_TMP_DESCEND" "$VAR_TMP_KEEP" <<'REAP'
import os
import shutil
import sys
import time

root, cap, min_age, descend, keep = sys.argv[1:6]
cap = int(cap)
min_age = int(min_age)
descend = set(descend.split())
keep = set(keep.split())

# Reap to a LOW-WATER MARK rather than to the ceiling itself, so a tree hovering at the
# boundary does not evict one entry on every single tick.
target = cap * 80 // 100


def measure(path):
    """(bytes, newest mtime) over ``path``. Symlinks are never followed and never sized: a
    dangling or absolute one would otherwise attribute somebody else's bytes here, and
    following one is how a reaper deletes outside its own tree."""
    total = 0
    newest = 0.0
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0.0
    newest = st.st_mtime
    if not os.path.isdir(path) or os.path.islink(path):
        return (st.st_size if os.path.isfile(path) and not os.path.islink(path) else 0), newest
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        try:
            newest = max(newest, os.lstat(dirpath).st_mtime)
        except OSError:
            pass
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            if not os.path.islink(os.path.join(dirpath, name)):
                total += st.st_size
    return total, newest


def candidates():
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        print(f"vartmp-cap: cannot list {root}: {exc}", file=sys.stderr)
        raise SystemExit(0)
    for name in names:
        if name in keep:
            continue
        path = os.path.join(root, name)
        if name in descend and os.path.isdir(path) and not os.path.islink(path):
            try:
                children = sorted(os.listdir(path))
            except OSError:
                continue
            for child in children:
                yield os.path.join(path, child)
            continue
        yield path


now = time.time()
entries = []
total = 0
for path in candidates():
    size, newest = measure(path)
    total += size
    entries.append((newest, size, path))

if total <= cap:
    raise SystemExit(0)

entries.sort(key=lambda item: item[0])  # oldest first
removed = []
protected = 0
for newest, size, path in entries:
    if total <= target:
        break
    if now - newest < min_age:
        protected += size
        continue
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=False)
        else:
            os.unlink(path)
    except OSError as exc:
        print(f"vartmp-cap: could not remove {path}: {exc}", file=sys.stderr)
        continue
    total -= size
    removed.append((path, size))

for path, size in removed:
    print(f"vartmp-cap: reaped {path} ({size}B)", file=sys.stderr)

if total > cap:
    print(
        f"vartmp-cap: WARNING — {root} is still {total}B against a {cap}B budget after "
        f"reaping {len(removed)} entr{'y' if len(removed) == 1 else 'ies'}; "
        f"{protected}B is inside the {min_age}s grace window and cannot be evicted. This is "
        "the reaper's fill-rate assumption failing, not a ceiling being enforced — see "
        "infra/runbooks/review-bot-ops.md",
        file=sys.stderr,
    )
REAP
}

# --- Applying the quota ----------------------------------------------------
# ONLY when the kernel is already accounting. The precondition needs a reboot and this script
# never takes one; when it is absent the warning carries the exact operator steps, because a
# message that says "quota unavailable" and stops is a dead end.
apply_quota() {
  if ! command -v xfs_quota >/dev/null 2>&1; then
    warn "xfs_quota is not installed, so no HARD ceiling can be applied to ${VAR_TMP_DIR}; \
install xfsprogs, then see the rootflags=pquota + reboot procedure in infra/runbooks/review-bot-ops.md"
    return 0
  fi
  if ! quota_accounting_on; then
    warn "WARNING — the kernel is NOT accounting XFS project quota on ${VAR_TMP_QUOTA_FS}, so \
${VAR_TMP_DIR} has NO hard byte ceiling; it is held only by ${REAPER_UNIT}.timer, a mitigation \
with a fill-rate assumption. XFS reads quota options at MOUNT time and refuses to enable them on \
a remount, so enabling this is an operator action: append rootflags=pquota to \
GRUB_CMDLINE_LINUX in /etc/default/grub, regenerate grub, and reboot the host (a scheduled \
Gerrit outage). Full procedure: infra/runbooks/review-bot-ops.md"
    return 0
  fi
  xfs_quota -x -c "project -s -p ${VAR_TMP_DIR} ${VAR_TMP_PROJECT_ID}" "$VAR_TMP_QUOTA_FS" \
    2>/dev/null ||
    warn "could not define XFS project ${VAR_TMP_PROJECT_ID} for ${VAR_TMP_DIR}"
  xfs_quota -x -c "limit -p bhard=${VAR_TMP_MAX_BYTES} ${VAR_TMP_PROJECT_ID}" \
    "$VAR_TMP_QUOTA_FS" 2>/dev/null ||
    warn "could not set the ${VAR_TMP_MAX_BYTES}B hard limit on XFS project ${VAR_TMP_PROJECT_ID}"
}

# Report, in the strongest terms the evidence supports, which regime the box is in.
report_state() {
  if quota_enforced; then
    warn "a HARD XFS project quota is being ENFORCED on ${VAR_TMP_DIR} at ${VAR_TMP_MAX_BYTES}B"
  else
    warn "NOTE — ${VAR_TMP_DIR} has no hard byte ceiling; the ${VAR_TMP_MAX_BYTES}B budget is a \
timer-driven mitigation that bounds STEADY accumulation and can be defeated by a single writer \
sustaining more than roughly cap/period bytes per second"
  fi
  if cleanup_in_effect; then
    warn "age cleanup and ${REAPER_UNIT}.timer are both in force"
  else
    warn "WARNING — the ${VAR_TMP_DIR} cleanup is NOT in force (drop-in missing/stale, or \
${REAPER_UNIT}.timer is not running); nothing is bounding ${VAR_TMP_DIR} at all"
  fi
}

# --- Argument handling -----------------------------------------------------
mode=""
while [ $# -gt 0 ]; do
  case "$1" in
    --print-env | --print-conf | --print-units | --check-active | --check-quota | --reap | --install)
      mode="$1"
      ;;
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
    # percent-of-cap and the budget the box is configured to hold are the same number by
    # construction and cannot drift apart.
    printf 'VAR_TMP_MAX_BYTES=%s\n' "$VAR_TMP_MAX_BYTES"
    printf 'VAR_TMP_DIR=%s\n' "$VAR_TMP_DIR"
    printf 'VAR_TMP_TMPFILES_CONF=%s\n' "$VAR_TMP_TMPFILES_CONF"
    printf 'VAR_TMP_MAX_AGE_DAYS=%s\n' "$VAR_TMP_MAX_AGE_DAYS"
    printf 'VAR_TMP_MIN_AGE_SECONDS=%s\n' "$VAR_TMP_MIN_AGE_SECONDS"
    exit 0
    ;;
  --print-conf)
    render_tmpfiles_conf
    exit 0
    ;;
  --print-units)
    render_units
    exit 0
    ;;
  --check-active)
    if cleanup_in_effect; then printf '1\n'; else printf '0\n'; fi
    exit 0
    ;;
  --check-quota)
    if quota_enforced; then printf '1\n'; else printf '0\n'; fi
    exit 0
    ;;
  --reap)
    reap
    exit 0
    ;;
esac

# --- Install ---------------------------------------------------------------
rendered="$(render_tmpfiles_conf)" || die "could not render ${VAR_TMP_TMPFILES_CONF}"

conf_dir="$(dirname "$VAR_TMP_TMPFILES_CONF")"
mkdir -p "$conf_dir" || die "cannot create ${conf_dir}"

# Bound the candidate's lifetime AT CREATION rather than on each exit path: an interrupt between
# the write and the `mv` would otherwise leave a half-considered config in systemd's own
# tmpfiles.d directory, where systemd-tmpfiles WOULD read it.
candidate="${VAR_TMP_TMPFILES_CONF}.candidate.$$"
trap 'rm -f "$candidate"' EXIT INT TERM

if [ -f "$VAR_TMP_TMPFILES_CONF" ] && [ "$rendered" = "$(cat "$VAR_TMP_TMPFILES_CONF" 2>/dev/null)" ]; then
  warn "${VAR_TMP_TMPFILES_CONF} already ages ${VAR_TMP_DIR} out at ${VAR_TMP_MAX_AGE_DAYS}d; nothing to write"
else
  printf '%s\n' "$rendered" >"$candidate" || die "cannot write ${candidate}"
  mv -f "$candidate" "$VAR_TMP_TMPFILES_CONF" || die "could not install ${VAR_TMP_TMPFILES_CONF}"
  warn "installed ${VAR_TMP_MAX_AGE_DAYS}d age cleanup for ${VAR_TMP_DIR} at ${VAR_TMP_TMPFILES_CONF}"
fi

# Apply the age rule NOW rather than waiting for the next systemd-tmpfiles-clean run, so a
# deploy that installs the rule also gets its first reclaim. `--clean` is the age verb;
# `--create` would only make the directory.
if command -v systemd-tmpfiles >/dev/null 2>&1; then
  systemd-tmpfiles --clean "$VAR_TMP_TMPFILES_CONF" 2>/dev/null ||
    warn "systemd-tmpfiles --clean did not complete; the timer will apply the rule on its own schedule"
fi

# The reaper unit runs from a copy under ${VAR_TMP_INSTALLED_PATH}, following
# install-observability.sh, so the unit does not depend on the checkout staying where it is. If
# that copy cannot be made the unit points at the script in place and says so — a reaper running
# from the checkout is worth having; a unit pointing at nothing is not.
if install -m 0755 "$SCRIPT_PATH" "$VAR_TMP_INSTALLED_PATH" 2>/dev/null; then
  EXEC_PATH="$VAR_TMP_INSTALLED_PATH"
else
  warn "could not copy this script to ${VAR_TMP_INSTALLED_PATH}; ${REAPER_UNIT}.service will run it from ${SCRIPT_PATH}"
fi

write_units "$VAR_TMP_UNIT_DIR" ||
  warn "could not write the ${REAPER_UNIT} units into ${VAR_TMP_UNIT_DIR}"

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload 2>/dev/null ||
    warn "systemctl daemon-reload failed; the reaper units may not be visible until the next reload"
  systemctl enable --now "${REAPER_UNIT}.timer" 2>/dev/null ||
    warn "could not enable ${REAPER_UNIT}.timer; ${VAR_TMP_DIR} is bounded by age cleanup alone"
fi

apply_quota
report_state
exit 0
