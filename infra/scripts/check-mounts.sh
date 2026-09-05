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

# Defaults. These must agree with terraform's `var.gate_scratch_mount` and with
# user_data.sh's mount points; tests/unit/test_user_data_device_resolution_d614.py
# pins the agreement so a change to one side fails offline rather than on a host.
DEFAULT_MOUNTS="/var/gerrit /var/lib/rebar/gate-scratch"

is_mounted() {
  if command -v mountpoint >/dev/null 2>&1; then
    mountpoint -q "$1"
    return
  fi
  # Fallback for a host without util-linux's mountpoint(1).
  awk -v mp="$1" '$2 == mp { found = 1 } END { exit found ? 0 : 1 }' /proc/mounts
}

describe_source() {
  if command -v findmnt >/dev/null 2>&1; then
    findmnt -no SOURCE "$1" 2>/dev/null && return 0
  fi
  awk -v mp="$1" '$2 == mp { print $1; exit }' /proc/mounts
}

if [ "$#" -gt 0 ]; then
  targets="$*"
else
  targets="$DEFAULT_MOUNTS"
fi

failed=0
for mount_point in $targets; do
  if is_mounted "$mount_point"; then
    printf 'OK    %s <- %s\n' "$mount_point" "$(describe_source "$mount_point")"
  else
    # Distinguish the two ways this goes wrong, because they need different
    # actions: an absent directory means the volume was never provisioned here,
    # while a PRESENT directory is the dangerous one — it looks healthy to every
    # consumer and is silently on the root filesystem.
    if [ -d "$mount_point" ]; then
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
