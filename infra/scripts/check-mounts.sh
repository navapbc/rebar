#!/usr/bin/env bash
# ===========================================================================
# check-mounts.sh — confirm the host's dedicated EBS volumes are IN SERVICE
# ===========================================================================
# Run this after ANY instance stop/start, instance replacement, or volume
# restore, BEFORE declaring the host healthy [rebar:9c93-754e-b641-48d1].
#
#   sudo infra/scripts/check-mounts.sh
#   sudo infra/scripts/check-mounts.sh /var/gerrit          # just one
#
# WHY it is needed even though user_data.sh already asserts the same thing:
# user_data.sh runs at FIRST BOOT only. Every later boot re-runs `mount -a`
# from /etc/fstab with nobody watching, and both entries carry `nofail` — which
# is deliberate (a missing volume must not wedge boot) and which means `mount -a`
# exits 0 whether or not the mount happened. An unmounted volume therefore leaves
# an ordinary directory behind, every service keeps working on the ROOT
# filesystem, and nothing says so: Gerrit fills the root disk while the DLM
# snapshots faithfully back up an empty data volume.
#
# Do NOT use "Gerrit responds" as evidence — it responds either way.
#
# PORTABLE BY CONSTRUCTION: plain shell plus util-linux, with a /proc/mounts
# fallback. No AWS API calls (so it works with no credentials, mid-incident, on
# a host whose IMDS is unhappy) and no CI provider (it is an operator command,
# and any scheduler can run it).
set -euo pipefail

# Defaults, applied by SETTING the argument list when none was given, so the loop
# below always iterates "$@" and never a word-split string. A mount point is a
# PATH: it may contain whitespace or a glob metacharacter, and splitting one into
# two bogus targets would make this check report on directories that do not exist
# while silently never checking the real one. These must agree with terraform's
# `var.gate_scratch_mount` and with user_data.sh's mount points;
# tests/unit/test_user_data_device_resolution_d614.py pins the agreement so a
# change to one side fails offline rather than on a host.
if [ "$#" -eq 0 ]; then
  set -- /var/gerrit /var/lib/rebar/gate-scratch
fi

# THREE outcomes, not two: mounted (0), not mounted (1), and CANNOT TELL (2). The third is
# not pedantry — without it a host with neither mountpoint(1) nor a readable /proc/mounts
# reports "this is a plain directory on the ROOT filesystem", which is a confident claim the
# script did not establish. Announcing an unverified state as fact is the exact failure this
# whole check exists to catch, so it must not be how the check itself behaves.
is_mounted() {
  if command -v mountpoint >/dev/null 2>&1; then
    mountpoint -q "$1"
    return
  fi
  # Fallback for a host without util-linux's mountpoint(1).
  if [ -r /proc/mounts ]; then
    awk -v mp="$1" '$2 == mp { found = 1 } END { exit found ? 0 : 1 }' /proc/mounts
    return
  fi
  return 2
}

# ALWAYS succeeds, printing the source or nothing. It is called in a command substitution
# under `set -e`, so a non-zero return here would abort the whole script instead of letting
# the caller report an undetermined source -- which made the empty-source branch below
# unreachable on any host where findmnt misses and /proc/mounts is absent.
describe_source() {
  if command -v findmnt >/dev/null 2>&1; then
    if findmnt -no SOURCE "$1" 2>/dev/null; then
      return 0
    fi
  fi
  if [ -r /proc/mounts ]; then
    awk -v mp="$1" '$2 == mp { print $1; exit }' /proc/mounts
  fi
  return 0
}

failed=0
for mount_point in "$@"; do
  if is_mounted "$mount_point"; then
    # The source is REPORTED, never assumed: `findmnt` exits non-zero on an
    # unusual mount and the /proc/mounts fallback can print nothing, and an
    # `OK <mp> <- ` line with a blank source reads as a partial read rather than
    # a pass. Saying which of the two happened is the whole point of this script,
    # so it says so rather than emitting a line that could mean either.
    mount_source=$(describe_source "$mount_point")
    if [ -n "$mount_source" ]; then
      printf 'OK    %s <- %s\n' "$mount_point" "$mount_source"
    else
      printf 'OK    %s is mounted (source could not be determined)\n' "$mount_point"
    fi
  else
    # Distinguish the ways this goes wrong, because they need different actions:
    # an absent directory means the volume was never provisioned here, while a
    # PRESENT directory is the dangerous one — it looks healthy to every consumer
    # and is silently on the root filesystem. "Cannot tell" is reported as itself.
    state=$?
    if [ "$state" -eq 2 ]; then
      printf 'FAIL  %s: cannot determine mount state (no mountpoint(1) and no readable /proc/mounts)\n' \
        "$mount_point" >&2
    elif [ -d "$mount_point" ]; then
      printf 'FAIL  %s is a plain directory on the ROOT filesystem, NOT a mount point\n' \
        "$mount_point" >&2
    else
      printf 'FAIL  %s does not exist -- no volume is mounted there\n' "$mount_point" >&2
    fi
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "" >&2
  echo "One or more volumes are NOT in service. Do not declare this host healthy." >&2
  echo "Check the attachment with 'lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,SERIAL' and the" >&2
  echo "fstab entry with 'grep -n \" \\(/var/gerrit\\|/var/lib/rebar/gate-scratch\\) \" /etc/fstab'." >&2
  echo "Recovery procedures: infra/runbooks/provision-restore.md and review-bot-ops.md." >&2
  exit 1
fi

echo "All expected volumes are mounted."
