#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# docker-storage-cap.sh — the ONE place that says how big /var/lib/docker may get
# (ADR 0112 decision 1, story 9183-aaae-667d-45e6).
#
# The 2026-09-02 outage filled the box's ROOT volume and took Gerrit, the review-bot's
# LLM-Review votes and the on-box MCP server down for ~5h. `/var/lib/docker` was 17G of the
# 28G working set — `overlay2` alone 16G across 67 layer directories — and NOTHING bounded it.
#
# This script owns the budget and installs the only cap the DAEMON itself enforces.
#
# ## One budget, three different enforcement strengths — stated honestly
#
# ADR 0112 is explicit that S2 (image/build-cache) and S5 (writable container layers) are ONE
# budget with an internal split, not two independent caps: writable layers live INSIDE
# `overlay2`, so two caps over the same bytes are either double-counted or mutually violable.
# So there is a single DOCKER_BUDGET_BYTES here, and the shares are derived from it — never a
# second literal that a later edit can drift out of agreement.
#
#   1. BUILDKIT SHARE — capped by the daemon's OWN garbage collector (`builder.gc` in
#      daemon.json), at the source, on the daemon's own schedule. This is the only part of the
#      budget that is enforced without anything else having to run.
#   2. IMAGE/LAYER SHARE — bounded by RETENTION, because Docker exposes no image-store
#      ceiling: `docker image prune -f` for dangling layers plus autodeploy.sh's
#      `mcp_reconcile_orphans`, which sweeps superseded per-release tags while preserving the
#      live backend and the recorded previous release. Weaker than a cap, and said so.
#   3. ORPHANED overlay2 — bounded by NOTHING, and that is the point. At the outage
#      `docker system df` reported ~9.5 GB with ZERO dangling images against 16G of real
#      overlay2, so ~6.5 GB was invisible to Docker's own accounting and unreachable by any
#      prune (four rounds recovered ~1.06 GB against a 29 GB problem). It cannot be capped —
#      it can only be MEASURED and alarmed, which observability.sh §2f does.
#
# ## Why the rendered key depends on the engine version
#
# Docker Engine 25.0 / BuildKit 0.13 introduced `builder.gc.maxUsedSpace` (with
# `reservedSpace`/`minFreeSpace`) and deprecated `defaultKeepStorage`. A key the running
# daemon does not recognise is SILENTLY IGNORED: the config looks installed, the cap does not
# exist, and the box reads healthy until it fills. So the version is PROBED and the schema
# that engine honours is what gets written. An unreadable version renders the modern schema
# and says so on stderr — worst case "no new cap plus a loud log", never a broken daemon.
#
# ## Usage
#
#   docker-storage-cap.sh --print-env                       # budget + split, for other scripts
#   docker-storage-cap.sh --print-json [--engine-version V] # the merged daemon.json, no writes
#   docker-storage-cap.sh --install                         # backup, validate, install, reload
#
# `--print-env` and `--print-json` are SIDE-EFFECT-FREE (the `compose-up.sh --print-volumes`
# precedent) so the rendering can be tested without a daemon or a writable /etc.
# ---------------------------------------------------------------------------
set -uo pipefail

# --- The budget ------------------------------------------------------------
# MEASURED defaults, operator-settable (ADR 0112 decision 6 — a default is a starting point
# sized from one host's measurement, never a frozen constant):
#   * 20 GiB total. Root is 60 GiB and the measured Docker working set at the outage was 17G;
#     20 GiB gives the generator explicit room while leaving the OS, /var/tmp (S4) and
#     /var/log (S3) their own shares of the volume.
#   * 5 GiB BuildKit. NOT a new number — it is autodeploy.sh's existing BUILD_CACHE_KEEP=5GB,
#     promoted to one place so the on-demand prune and the daemon's GC cannot disagree.
DOCKER_BUDGET_BYTES="${DOCKER_BUDGET_BYTES:-21474836480}"          # 20 GiB
DOCKER_BUILDKIT_CACHE_BYTES="${DOCKER_BUILDKIT_CACHE_BYTES:-5368709120}"  # 5 GiB
DOCKER_ROOT="${DOCKER_ROOT:-/var/lib/docker}"
DOCKER_DAEMON_JSON="${DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"

#: Engine major version from which `maxUsedSpace` replaces `defaultKeepStorage`.
DOCKER_MODERN_GC_MAJOR=25

#: How many timestamped `daemon.json.bak.<epoch>` copies `--install` keeps. Deliberately a
#: constant rather than a knob: this is a DISK-CEILING script, and the one thing it must not
#: do is grow an unbounded on-disk set of its own.
DOCKER_DAEMON_JSON_BACKUPS_KEEP=5

#: Where a running process's start time is read from. Exists as a seam so the activation
#: check below is testable without a live dockerd; nothing in production overrides it.
DOCKER_PROC_DIR="${DOCKER_PROC_DIR:-/proc}"

die() { printf 'docker-storage-cap: %s\n' "$*" >&2; exit 1; }
warn() { printf 'docker-storage-cap: %s\n' "$*" >&2; }

case "$DOCKER_BUDGET_BYTES" in ''|*[!0-9]*) die "DOCKER_BUDGET_BYTES must be an integer byte count" ;; esac
case "$DOCKER_BUILDKIT_CACHE_BYTES" in ''|*[!0-9]*) die "DOCKER_BUILDKIT_CACHE_BYTES must be an integer byte count" ;; esac
# A split that does not fit inside its own budget is a typo, not a policy: the two shares are
# ONE budget by construction, so refusing here is what keeps them from becoming two caps.
if [ "$DOCKER_BUILDKIT_CACHE_BYTES" -ge "$DOCKER_BUDGET_BYTES" ]; then
  die "BuildKit share ${DOCKER_BUILDKIT_CACHE_BYTES}B does not fit inside the /var/lib/docker budget ${DOCKER_BUDGET_BYTES}B"
fi
DOCKER_IMAGE_SHARE_BYTES=$((DOCKER_BUDGET_BYTES - DOCKER_BUILDKIT_CACHE_BYTES))

# --- Is the installed policy actually in force? ------------------------------
# `builder.gc` only becomes real when a dockerd process READS it, which happens at daemon
# startup and nowhere else. dockerd's SIGHUP reload (`systemctl reload docker`) applies a
# fixed set of keys and `builder.gc` is NOT among them: the reload exits 0 and the GC policy
# is unchanged. An earlier revision of this script reported "the new builder.gc policy is in
# effect" on that exit status — ABSENCE OF EXECUTION REPORTED AS SUCCESS, the same defect
# class as bugs 9a17, 90c7 and 1ef8. A cap that reports itself installed while not in force is
# worse than no cap at all, because the loud half manufactures confidence.
#
# So nothing here infers activation from a command's exit status. Activation is OBSERVED:
# a live daemon is enforcing this file only if it started AFTER the file was last written.

# Epoch seconds of $1's mtime, or non-zero when it cannot be read. python3 rather than `stat`,
# whose format flags differ between GNU and BSD; python3 is already required to render.
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

# Epoch seconds at which the LIVE dockerd process started, or non-zero when that cannot be
# established. `/proc/<pid>` is created by the kernel when the process is, so its mtime dates
# the running daemon — not the unit file, not the package, not the last reload.
docker_started_at() {
  local pid
  pid="$(systemctl show docker --property=MainPID --value 2>/dev/null | tr -dc '0-9')"
  case "$pid" in ''|0) return 1 ;; esac
  mtime_of "${DOCKER_PROC_DIR}/${pid}"
}

# Report, in the strongest terms the evidence supports, whether $DOCKER_DAEMON_JSON is the
# policy the running daemon is enforcing. FAILS CLOSED: anything undeterminable is reported as
# NOT in effect, because the cost of over-claiming here is an unbounded disk.
report_activation_state() {
  local started written
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl is-active --quiet docker 2>/dev/null; then
    # compose-up.sh installs the policy BEFORE `systemctl enable --now docker`, so on a first
    # boot this is the ordinary path and the next start reads the file for free.
    warn "Docker is not running; it will read the ${schema} builder.gc policy when it next starts"
    return 0
  fi
  written="$(mtime_of "$DOCKER_DAEMON_JSON")" || written=""
  started="$(docker_started_at)" || started=""
  if [ -n "$written" ] && [ -n "$started" ] && [ "$started" -ge "$written" ]; then
    warn "the live Docker daemon started after this policy was written, so the ${schema} builder.gc policy (BuildKit share ${DOCKER_BUILDKIT_CACHE_BYTES}B) IS in effect"
    return 0
  fi
  # Never an automatic restart: bouncing Docker takes Gerrit, the review-bot and the on-box
  # MCP server down with it, which is an operator's scheduling decision, not a boot script's.
  warn "WARNING — the ${schema} builder.gc policy is INSTALLED but is NOT in effect: the live Docker daemon predates it (or its start time could not be read), and dockerd's SIGHUP reload does not apply builder.gc. Docker is deliberately NOT restarted here; schedule the restart per infra/runbooks/review-bot-ops.md"
  return 0
}

# Bound the operator's undo set. Glob order is ascending lexicographic and the `.bak.<epoch>`
# suffix stays fixed-width for the next two centuries, so the shell already hands these back
# oldest-first and the excess to drop is the head of the list.
prune_daemon_json_backups() {
  local file drop
  local -a backups=()
  for file in "${DOCKER_DAEMON_JSON}".bak.*; do
    [ -f "$file" ] && backups+=("$file")
  done
  [ "${#backups[@]}" -gt "$DOCKER_DAEMON_JSON_BACKUPS_KEEP" ] || return 0
  drop=$(( ${#backups[@]} - DOCKER_DAEMON_JSON_BACKUPS_KEEP ))
  for file in "${backups[@]:0:$drop}"; do
    rm -f "$file" && warn "pruned superseded backup ${file} (keeping the ${DOCKER_DAEMON_JSON_BACKUPS_KEEP} most recent)"
  done
}

# --- Engine version --------------------------------------------------------
# Ask the running daemon first (the authority on what IT honours), then the binary (first
# boot, before `systemctl enable --now docker`). Both bounded: a wedged daemon must not hang
# a boot orchestrator or a 5-minute probe.
probe_engine_version() {
  local v
  v="$(docker version --format '{{.Server.Version}}' 2>/dev/null | tr -d '[:space:]')"
  if [ -n "$v" ]; then printf '%s\n' "$v"; return 0; fi
  v="$(dockerd --version 2>/dev/null | sed -n 's/.*version[[:space:]]*\([0-9][0-9.]*\).*/\1/p' | head -1)"
  [ -n "$v" ] && { printf '%s\n' "$v"; return 0; }
  return 1
}

# Echo `modern` or `legacy` for an engine version string.
gc_schema_for() {
  local major
  major="${1%%.*}"
  case "$major" in ''|*[!0-9]*) printf 'modern\n'; return 0 ;; esac
  if [ "$major" -ge "$DOCKER_MODERN_GC_MAJOR" ]; then printf 'modern\n'; else printf 'legacy\n'; fi
}

# --- Rendering -------------------------------------------------------------
# The merge is per-KEY, not per-object: daemon.json on this host is not ours alone, and
# replacing it wholesale is how a "storage cap" becomes an outage. Only `builder.gc`'s space
# keys are asserted; every sibling key, inside `builder` and outside it, is carried through.
render_daemon_json() {
  local schema="$1"
  DSC_SCHEMA="$schema" \
  DSC_CAP="$DOCKER_BUILDKIT_CACHE_BYTES" \
  DSC_PATH="$DOCKER_DAEMON_JSON" \
  python3 - <<'PY'
import json
import os
import sys

path = os.environ["DSC_PATH"]
# The cap is written as a STRING holding an exact byte count, which is what Docker's own
# daemon.json reference uses for these keys ("reservedSpace": "30GB"). Both the legacy
# `defaultKeepStorage` (a plain string field) and the modern `maxUsedSpace` (a DiskSpace,
# which unmarshals from a string or a number) parse it through go-units, where a bare digit
# string is bytes — so this is exact rather than unit-ambiguous, and it is what `dockerd
# --validate` checks below before the file is ever moved into place.
cap = os.environ["DSC_CAP"]
schema = os.environ["DSC_SCHEMA"]

try:
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
except FileNotFoundError:
    config = {}
except (OSError, ValueError) as exc:
    print(f"docker-storage-cap: {path} is unreadable or not JSON: {exc}", file=sys.stderr)
    raise SystemExit(1) from None

if not isinstance(config, dict):
    print(f"docker-storage-cap: {path} is not a JSON object", file=sys.stderr)
    raise SystemExit(1)

builder = config.get("builder")
if not isinstance(builder, dict):
    builder = {}
gc = builder.get("gc")
if not isinstance(gc, dict):
    gc = {}

gc["enabled"] = True
# Assert exactly ONE of the two space keys and drop the other, so a box upgraded across the
# 25.0 boundary cannot end up carrying a stale key beside the live one.
if schema == "modern":
    gc["maxUsedSpace"] = cap
    gc.pop("defaultKeepStorage", None)
else:
    gc["defaultKeepStorage"] = cap
    gc.pop("maxUsedSpace", None)

builder["gc"] = gc
config["builder"] = builder
print(json.dumps(config, indent=2, sort_keys=True))
PY
}

# --- Argument handling -----------------------------------------------------
mode=""
engine_version=""
while [ $# -gt 0 ]; do
  case "$1" in
    --print-env|--print-json|--install) mode="$1" ;;
    --engine-version) shift; engine_version="${1:-}" ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[ -n "$mode" ] || mode="--install"

if [ "$mode" = "--print-env" ]; then
  # Consumed with `eval "$(… --print-env)"` by autodeploy.sh and observability.sh, so that the
  # prune's --keep-storage, the published cap metrics and the daemon's own GC policy are all
  # the same number by construction.
  printf 'DOCKER_BUDGET_BYTES=%s\n' "$DOCKER_BUDGET_BYTES"
  printf 'DOCKER_BUILDKIT_CACHE_BYTES=%s\n' "$DOCKER_BUILDKIT_CACHE_BYTES"
  printf 'DOCKER_IMAGE_SHARE_BYTES=%s\n' "$DOCKER_IMAGE_SHARE_BYTES"
  printf 'DOCKER_ROOT=%s\n' "$DOCKER_ROOT"
  exit 0
fi

if [ -z "$engine_version" ]; then
  if ! engine_version="$(probe_engine_version)"; then
    engine_version=""
    warn "could not read the Docker engine version (daemon down and dockerd unreadable); rendering the >= ${DOCKER_MODERN_GC_MAJOR}.0 schema"
  fi
fi
schema="$(gc_schema_for "$engine_version")"

rendered="$(render_daemon_json "$schema")" || die "could not render ${DOCKER_DAEMON_JSON}"

if [ "$mode" = "--print-json" ]; then
  printf '%s\n' "$rendered"
  exit 0
fi

# --- Install ---------------------------------------------------------------
# No-op fast path FIRST: re-running the boot orchestrator must not bounce Docker to install
# bytes that are already there.
if [ -f "$DOCKER_DAEMON_JSON" ] && [ "$rendered" = "$(cat "$DOCKER_DAEMON_JSON" 2>/dev/null)" ]; then
  warn "${DOCKER_DAEMON_JSON} already carries the ${schema} builder.gc policy (BuildKit share ${DOCKER_BUILDKIT_CACHE_BYTES}B); nothing to write"
  # "Nothing to write" is NOT "the cap is in force" — this is the one path on which it may
  # genuinely be, so it is the one path worth checking rather than assuming.
  report_activation_state
  exit 0
fi

daemon_dir="$(dirname "$DOCKER_DAEMON_JSON")"
mkdir -p "$daemon_dir" || die "cannot create ${daemon_dir}"

# Validate the CANDIDATE, never the live file. A malformed daemon.json stops dockerd
# starting — a self-inflicted outage on the very host this cap exists to protect — so the
# render only reaches its final path after the daemon's own validator accepts it.
candidate="${DOCKER_DAEMON_JSON}.candidate.$$"
# Bound the candidate's lifetime AT CREATION rather than on each exit path: an interrupt
# between the write and the `mv` would otherwise leave a half-considered config lying in the
# daemon's own config directory, and every retry adds another PID-suffixed one.
trap 'rm -f "$candidate"' EXIT INT TERM
printf '%s\n' "$rendered" > "$candidate" || die "cannot write ${candidate}"

if command -v dockerd >/dev/null 2>&1; then
  if ! dockerd --validate --config-file "$candidate" >/dev/null 2>&1; then
    rm -f "$candidate"
    die "dockerd REJECTED the rendered ${DOCKER_DAEMON_JSON}; the existing file is untouched"
  fi
elif ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$candidate" >/dev/null 2>&1; then
  rm -f "$candidate"
  die "the rendered ${DOCKER_DAEMON_JSON} is not valid JSON; the existing file is untouched"
fi

# Keep what was there. The backup is the operator's undo for a policy that turns out wrong on
# this box, and it is taken BEFORE the replacement rather than reconstructed afterwards.
if [ -f "$DOCKER_DAEMON_JSON" ]; then
  backup="${DOCKER_DAEMON_JSON}.bak.$(date +%s)"
  if ! cp -p "$DOCKER_DAEMON_JSON" "$backup"; then
    rm -f "$candidate"
    die "could not back up ${DOCKER_DAEMON_JSON}; refusing to replace it"
  fi
  warn "backed up ${DOCKER_DAEMON_JSON} to ${backup}"
fi

mv -f "$candidate" "$DOCKER_DAEMON_JSON" || die "could not install ${DOCKER_DAEMON_JSON}"
warn "installed the ${schema} builder.gc policy (BuildKit share ${DOCKER_BUILDKIT_CACHE_BYTES}B of a ${DOCKER_BUDGET_BYTES}B /var/lib/docker budget)"
prune_daemon_json_backups

# No `systemctl reload` is attempted, and its removal is the point rather than a tidy-up: a
# SIGHUP reload cannot apply builder.gc, so the only thing it ever produced here was a zero
# exit status that read as activation. What follows OBSERVES the running daemon instead — and
# on this path (the file was just rewritten) it will correctly say NOT in effect until an
# operator restarts Docker.
report_activation_state
exit 0
