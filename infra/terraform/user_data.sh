#!/usr/bin/env bash
# ===========================================================================
# user_data.sh — cloud-init for the rebar Gerrit host (AL2023, arm64/Graviton)
# ===========================================================================
# Rendered by Terraform's templatefile(). The ONLY Terraform interpolation in
# this file is the data_volume_id, written with a SINGLE dollar + braces. Every
# LITERAL bash brace-expansion is escaped with a DOUBLE dollar + braces, so
# templatefile passes a single-dollar brace form through to the shell.
# IMPORTANT: the double-dollar escape only applies before a brace, so brace-LESS
# bash refs must be plain $VAR (a double-dollar $VAR would render literally and
# break). Likewise do NOT double-dollar the data_volume_id — that would emit the
# literal text and break NVMe device resolution.
# ===========================================================================
set -euo pipefail

# Defensive installs. AL2023 ships aws-cli v2 and nvme-cli, but don't assume.
dnf install -y nvme-cli || true

# ---------------------------------------------------------------------------
# 1) Resolve an EBS volume's NVMe device dynamically, by volume id.
# ---------------------------------------------------------------------------
# Nitro exposes EBS volumes as NVMe devices, not their requested attachment names. Resolve both
# Gerrit and gate-scratch volumes by the dashless EBS ID in the controller serial.
#
# Keep one shared resolver so both callers retain the by-id fallback when nvme-cli misses.
#
# Terraform substitutes volume IDs before execution. ShellCheck sees the unrendered template.

# Two refusals, each written as its OWN function called from a single line, so the guard is
# individually removable — which is what lets a test SEED the defect back in and prove the
# guard is load-bearing rather than incidental (bug d614-448f-a538-4cec, AC4).

# An EBS volume id is `vol-` plus exactly 8 (legacy) or 17 hex digits. The device matches
# below are SUBSTRING tests and what they return becomes a mkfs.xfs target, so a degenerate
# argument has to be refused BEFORE any device is examined rather than guessed at:
#   empty id  -> `grep -qi ""` matches EVERY device, so resolution returns whichever
#                /dev/nvme*n1 sorts first, with exit 0. Proven in a sandbox to mkfs an
#                unrelated volume and to append a permanent `UUID= ...` line to /etc/fstab.
#   truncated -> a prefix such as vol-0ddd matches the real serial by substring.
# `set -u` catches an UNSET variable; it does NOT catch an EMPTY one, and empty is the
# reachable case — infra/runbooks/review-bot-ops.md tells an operator to re-run these mount
# steps by hand, so this lands on a human under incident pressure beside the Gerrit data
# volume. Anchoring end to end also makes every accepted id a FIXED LENGTH, which is why no
# valid id can be a proper prefix of another — that is what keeps the substring test in the
# by-id fallback safe without disturbing a fallback proven to work when nvme-cli is broken.
require_well_formed_volume_id() {
  if printf '%s' "$1" | grep -qiE '^vol([0-9a-f]{8}|[0-9a-f]{17})$'; then
    return 0
  fi
  echo "refusing malformed EBS volume id '$2' -- will not guess a device" >&2
  return 2
}

# A device blkid RECOGNISES but has no UUID for — a GPT-partitioned disk, say — skips the
# mkfs below and used to fall straight through to appending
# `UUID= <mount> xfs defaults,nofail 0 2`. That malformed line persists in /etc/fstab
# forever, `mount -a` still exits 0, and so did mount_ebs_volume. Both call sites already
# treat a non-zero return as FATAL, so refusing here is a loud stop instead.
require_filesystem_uuid() {
  if [ -n "$2" ]; then
    return 0
  fi
  echo "$1 has no filesystem UUID -- refusing to write a malformed fstab entry" >&2
  return 1
}

# A `nofail` fstab entry is SKIPPED QUIETLY when its volume is missing -- that is what nofail
# is FOR, so that a missing volume cannot wedge boot. The cost is that `mount -a` exits 0
# whether or not the mount actually happened, so a successful `mount -a` is NOT evidence. A
# bare mount point is an ordinary directory: every consumer keeps working, on ROOT, and the
# volume's failure mode becomes the outage it was bought to prevent. So the mount is ASSERTED.
# Called for EVERY volume, not just gate scratch: the older /var/gerrit path had no assertion
# at all, which meant a silently-unmounted Gerrit data volume put the repositories on the 60
# GiB root disk while the DLM snapshots kept backing up the empty one (bug 9c93-754e-b641-48d1).
require_mounted() {
  if mountpoint -q "$1"; then
    return 0
  fi
  echo "$1 is NOT a mount point after a CLEAN mount -a -- its nofail fstab entry was skipped silently; refusing to leave it on the ROOT filesystem" >&2
  return 1
}

# Write the fstab entry for a mount point, REPLACING any entry that mount point already has.
# The previous guard was `grep -q "$uuid" /etc/fstab`, which only asked whether the NEW UUID
# was present, so replacing a volume -- which is exactly what the restore runbook's "restore
# onto a fresh volume" path does -- APPENDED a second line for the same mount point and left
# the old volume's line behind. `nofail` then hides the result: neither line errors, and which
# volume ends up at the mount point becomes fstab-ORDER dependent, permanently and silently
# (bug ad8d-4274-ef43-4f44 F3).
#
# The backup is not a nicety: it is what makes the truncating redirect below safe, and it is
# the file an operator restores from if a bad edit ever leaves the host unbootable. awk READS
# the backup and WRITES /etc/fstab, so the input is never the file being truncated. It holds
# the state BEFORE THIS CALL, not before the boot -- with two volumes the second call
# overwrites the first call's copy, which is what "recoverable from the last edit" means here.
replace_fstab_entry() {
  fstab_mount_point="$1"
  fstab_uuid="$2"
  cp -p /etc/fstab /etc/fstab.rebar-bak
  awk -v mp="$fstab_mount_point" '$2 != mp' /etc/fstab.rebar-bak > /etc/fstab # DROP-STALE-FSTAB-ENTRY
  printf '%s\n' "UUID=$fstab_uuid $fstab_mount_point xfs defaults,nofail 0 2" >> /etc/fstab
}

resolve_ebs_device() {
  vol_nodash=$(echo "$1" | tr -d '-')
  require_well_formed_volume_id "$vol_nodash" "$1" || return 2 # REFUSE-DEGENERATE-VOLUME-ID
  found=""

  for d in /dev/nvme*n1; do
    [ -e "$d" ] || continue
    # Anchored on non-alphanumeric boundaries so the id must be a WHOLE token in the identify
    # page rather than any substring of it — defense in depth behind the shape guard above.
    # The regex fragments are single-quoted, so no dollar-brace sequence reaches Terraform's
    # templatefile(), which interpolates this file whole (bug dd30-f10d-69f3-4c36).
    if nvme id-ctrl -v "$d" 2>/dev/null \
      | grep -qiE '(^|[^0-9a-zA-Z])'"$vol_nodash"'([^0-9a-zA-Z]|$)'; then
      # `readlink -f`, matching the by-id fallback below, so BOTH branches answer with the
      # same canonical string for the same device. They used to differ -- the glob path here,
      # the realpath there -- which made log lines and any future device COMPARISON depend on
      # which branch happened to fire (bug ad8d-4274-ef43-4f44 F5).
      found=$(readlink -f "$d")
      break
    fi
  done

  # Fallback: the by-id symlinks also embed the volume id in the serial.
  if [ -z "$found" ]; then
    for link in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*; do
      [ -e "$link" ] || continue
      case "$link" in
        *"$vol_nodash"*)
          found=$(readlink -f "$link")
          break
          ;;
      esac
    done
  fi

  [ -n "$found" ] || return 1
  printf '%s\n' "$found"
}

# Format (idempotent) + mount by UUID through fstab. Returns non-zero if the device
# cannot be resolved; the CALLER decides how loud that is, because the two volumes
# have different failure dispositions.
mount_ebs_volume() {
  volume_id="$1"
  mount_point="$2"
  if ! device=$(resolve_ebs_device "$volume_id"); then
    echo "could not resolve an NVMe device for volume $volume_id" >&2
    return 1
  fi

  echo "Resolved volume $volume_id -> $device"
  if ! blkid "$device" >/dev/null 2>&1; then
    echo "No filesystem on $device — creating xfs"
    mkfs.xfs "$device"
  fi

  mkdir -p "$mount_point"
  uuid=$(blkid -s UUID -o value "$device")
  require_filesystem_uuid "$device" "$uuid" || return 1 # REFUSE-EMPTY-FILESYSTEM-UUID
  replace_fstab_entry "$mount_point" "$uuid"

  # Two DISTINCT failures, reported distinctly, because nofail makes them look identical from
  # the outside: `mount -a` refusing is a real error it printed about, while `mount -a`
  # succeeding and the mount point still being an ordinary directory is the SILENT skip.
  # Telling them apart is what tells an operator whether to look at the filesystem or at the
  # volume attachment.
  if ! mount -a; then
    echo "mount -a reported an ERROR while mounting $mount_point" >&2
    return 3
  fi
  require_mounted "$mount_point" || return 4 # ASSERT-MOUNT-TOOK
}

# ---------------------------------------------------------------------------
# 2) Mount the Gerrit data volume at /var/gerrit.
# ---------------------------------------------------------------------------
# FAILS LOUD for the same reason the gate-scratch mount below does, and this is the volume
# where it matters MOST. `mount_ebs_volume` now asserts the mount took, so this branch covers
# the silent `nofail` skip as well as an unresolvable device -- the specific reason is on
# stderr immediately above. Gerrit left running on the ROOT filesystem is a 60 GiB disk that
# has already hit 97% once, and DLM snapshots that faithfully back up an empty data volume.
# shellcheck disable=SC2154
if ! mount_ebs_volume "${data_volume_id}" /var/gerrit; then
  echo "FATAL: volume ${data_volume_id} is not in service at /var/gerrit -- refusing to run Gerrit on the ROOT filesystem" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 2b) Mount the review-gate SCRATCH volume (ADR 0112 decision 3, story aa40).
# ---------------------------------------------------------------------------
# Snapshot data and review clones use this volume instead of the OS disk.
#
# A `nofail` skip leaves an ordinary directory on root while `mount -a` reports success.
# mount_ebs_volume therefore asserts both mounts and fails boot loudly. Terraform supplies
# these variables after ShellCheck runs, so each reference needs its adjacent directive.
# shellcheck disable=SC2154
GATE_SCRATCH_MOUNT="${gate_scratch_mount}"
# shellcheck disable=SC2154
if ! mount_ebs_volume "${gate_scratch_volume_id}" "$GATE_SCRATCH_MOUNT"; then
  echo "FATAL: gate-scratch volume ${gate_scratch_volume_id} is not in service at $GATE_SCRATCH_MOUNT -- refusing to leave gate scratch on the ROOT filesystem" >&2
  exit 1
fi

# The two marker files rebar's gate admission reads (rebar.llm.gate_admission).
# Root-side `.gate-scratch-required` declares the volume. Volume-side
# `.gate-scratch-mounted` proves it is mounted. Declaration without proof makes admission
# refuse instead of repopulating root. Tighten the mount before writing either marker.
chmod 0700 "$GATE_SCRATCH_MOUNT"
touch "$GATE_SCRATCH_MOUNT/../.gate-scratch-required"
touch "$GATE_SCRATCH_MOUNT/.gate-scratch-mounted"
echo "Gate scratch mounted at $GATE_SCRATCH_MOUNT and marked"

# ---------------------------------------------------------------------------
# 3) Fetch required /rebar/prod SecureStrings into /etc/rebar/.env (0600).
#    A CHANGEME sentinel is boot-fatal.
# ---------------------------------------------------------------------------
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region \
  -H "X-aws-ec2-metadata-token: $TOKEN")

mkdir -p /etc/rebar
ENV_FILE=/etc/rebar/.env
umask 077
: > "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Map parameter names to environment keys. `$${...}` is required for literal Bash brace
# expansions even in comments because templatefile parses the whole file and renders `$$`
# as `$`. Iteration uses `$${!PARAMS[@]}` / `$${PARAMS[$name]}`. Only declared
# `${data_volume_id}` is a Terraform interpolation. ShellCheck sees the pre-render form.
# shellcheck disable=SC2034
declare -A PARAMS=(
  ["/rebar/prod/gerrit-admin-password"]="GERRIT_ADMIN_PASSWORD"
  ["/rebar/prod/gerrit-ssh-host-ed25519-key"]="GERRIT_SSH_HOST_ED25519_KEY"
  ["/rebar/prod/github-replication-deploy-key"]="GITHUB_REPLICATION_DEPLOY_KEY"
  ["/rebar/prod/mcp-hmac-signing-key"]="MCP_HMAC_SIGNING_KEY"
  ["/rebar/prod/anthropic-api-key"]="ANTHROPIC_API_KEY"
  ["/rebar/prod/alert-endpoint"]="ALERT_ENDPOINT"
  ["/rebar/prod/gerrit-bot-token"]="GERRIT_BOT_TOKEN"
  # OAuth credentials are intentionally excluded. Their consumers read compose .env only when
  # OAUTH is enabled. Fetching them here would make unused placeholders boot-fatal.
)

# `$${!PARAMS[@]}` renders one word per key. ShellCheck sees the literal and misdiagnoses it.
# shellcheck disable=SC2066
for name in "$${!PARAMS[@]}"; do
  key="$${PARAMS[$name]}"
  value=$(aws ssm get-parameter --region "$REGION" --name "$name" \
    --with-decryption --query 'Parameter.Value' --output text)

  if [ "$value" = "CHANGEME" ]; then
    echo "FATAL: SSM parameter $name is still the CHANGEME placeholder — an operator must populate it before launch." >&2
    exit 1
  fi

  # Write KEY='value' with single quotes; embedded single quotes escaped.
  esc=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
  echo "$key='$esc'" >> "$ENV_FILE"
done

echo "Wrote $ENV_FILE with $${#PARAMS[@]} secrets (0600)."
echo "user_data.sh complete."
