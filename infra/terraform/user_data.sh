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
# Nitro/Graviton presents EBS volumes as /dev/nvme*n1, NOT the /dev/sdf/sdg we ask
# for in the attachment. We match by the EBS volume id, which AWS encodes (minus
# dashes) in the NVMe controller serial number.
#
# ONE function, two callers (the Gerrit data volume and the review-gate scratch
# volume added by story aa40-cbda-ee38-481c). A second inline copy of this loop is
# how the two would drift: the by-id fallback below exists because the nvme-cli path
# has been observed to miss, and a copy that lacks it fails on exactly the boot the
# fallback was added for.
#
# Terraform's templatefile() substitutes the volume ids before this script is ever
# executed. ShellCheck lints the UNRENDERED template and cannot know that.
resolve_ebs_device() {
  vol_nodash=$(echo "$1" | tr -d '-')
  found=""

  for d in /dev/nvme*n1; do
    [ -e "$d" ] || continue
    if nvme id-ctrl -v "$d" 2>/dev/null | grep -qi "$vol_nodash"; then
      found="$d"
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
  device=$(resolve_ebs_device "$volume_id") || return 1

  echo "Resolved volume $volume_id -> $device"
  if ! blkid "$device" >/dev/null 2>&1; then
    echo "No filesystem on $device — creating xfs"
    mkfs.xfs "$device"
  fi

  mkdir -p "$mount_point"
  uuid=$(blkid -s UUID -o value "$device")
  if ! grep -q "$uuid" /etc/fstab; then
    echo "UUID=$uuid $mount_point xfs defaults,nofail 0 2" >> /etc/fstab
  fi
  mount -a
}

# ---------------------------------------------------------------------------
# 2) Mount the Gerrit data volume at /var/gerrit.
# ---------------------------------------------------------------------------
# shellcheck disable=SC2154
if ! mount_ebs_volume "${data_volume_id}" /var/gerrit; then
  echo "FATAL: could not resolve NVMe device for volume ${data_volume_id}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 2b) Mount the review-gate SCRATCH volume (ADR 0112 decision 3, story aa40).
# ---------------------------------------------------------------------------
# Snapshot store + review-bot clones live here instead of on the root filesystem,
# so a review burst can no longer wedge the OS disk (bug 3276).
#
# FAILS LOUD, and that is the point. A bare mount point is an ordinary directory:
# if the mount silently does not take, every consumer keeps working — on root —
# and the volume's failure mode becomes exactly the outage it was built to
# prevent. `mount -a` alone does not prove the mount happened (a `nofail` entry is
# skipped quietly), so the mount is ASSERTED with `mountpoint` afterwards.
# Both names are terraform template variables substituted before this ever runs, and
# ShellCheck lints the UNRENDERED template — so each reference needs its own directive
# (a disable comment covers only the line that follows it).
# shellcheck disable=SC2154
GATE_SCRATCH_MOUNT="${gate_scratch_mount}"
# shellcheck disable=SC2154
if ! mount_ebs_volume "${gate_scratch_volume_id}" "$GATE_SCRATCH_MOUNT"; then
  echo "FATAL: could not resolve NVMe device for gate-scratch volume ${gate_scratch_volume_id}" >&2
  exit 1
fi
if ! mountpoint -q "$GATE_SCRATCH_MOUNT"; then
  echo "FATAL: $GATE_SCRATCH_MOUNT is not a mount point after mount -a — refusing to leave gate scratch on the ROOT filesystem" >&2
  exit 1
fi

# The two marker files rebar's gate admission reads (rebar.llm.gate_admission).
# They are on DIFFERENT filesystems on purpose, which is what makes "mounted" and
# "unmounted" tellable apart at all:
#   .gate-scratch-required — beside the mount point, on ROOT: the DECLARATION that
#     this host has a dedicated scratch volume. Survives an unmount.
#   .gate-scratch-mounted  — inside the mount point, on the VOLUME: the PROOF.
#     Disappears with the volume.
# Declaration present + proof absent => gate admission refuses instead of quietly
# repopulating the store on root.
touch "$GATE_SCRATCH_MOUNT/../.gate-scratch-required"
touch "$GATE_SCRATCH_MOUNT/.gate-scratch-mounted"
chmod 0700 "$GATE_SCRATCH_MOUNT"
echo "Gate scratch mounted at $GATE_SCRATCH_MOUNT and marked"

# ---------------------------------------------------------------------------
# 3) Fetch the SecureString secrets from SSM (instance role grants read on
#    /rebar/prod/*) and write /etc/rebar/.env (0600). FAIL FAST on the CHANGEME
#    sentinel — never write a half-configured env that silently misbehaves.
# ---------------------------------------------------------------------------
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region \
  -H "X-aws-ec2-metadata-token: $TOKEN")

mkdir -p /etc/rebar
ENV_FILE=/etc/rebar/.env
umask 077
: > "$ENV_FILE"
chmod 600 "$ENV_FILE"

# param name -> env var key. (Brace expansions below are escaped as $${...}
# because they survive templatefile to run in bash.)
# PARAMS is consumed below as $${!PARAMS[@]} / $${PARAMS[$name]}; templatefile turns
# each $$ into a literal $, so bash receives a real brace expansion.
# Do NOT spell the post-render form out in prose here. templatefile() interpolates the
# WHOLE file -- comments included, since # means nothing to it -- so an unescaped brace
# expansion in a COMMENT is parsed as HCL and breaks every terraform operation in the
# repo, not just this file (bug dd30-f10d-69f3-4c36; -target does not help, because
# terraform evaluates the whole configuration first). Only $${...} is safe in this file;
# the sole exception is ${data_volume_id}, which main.tf actually declares.
# ShellCheck reads the escaped pre-render form and so cannot see the use.
# shellcheck disable=SC2034
declare -A PARAMS=(
  ["/rebar/prod/gerrit-admin-password"]="GERRIT_ADMIN_PASSWORD"
  ["/rebar/prod/gerrit-ssh-host-ed25519-key"]="GERRIT_SSH_HOST_ED25519_KEY"
  ["/rebar/prod/github-replication-deploy-key"]="GITHUB_REPLICATION_DEPLOY_KEY"
  ["/rebar/prod/mcp-hmac-signing-key"]="MCP_HMAC_SIGNING_KEY"
  ["/rebar/prod/anthropic-api-key"]="ANTHROPIC_API_KEY"
  ["/rebar/prod/alert-endpoint"]="ALERT_ENDPOINT"
  ["/rebar/prod/gerrit-bot-token"]="GERRIT_BOT_TOKEN"
  # NOTE: the GitHub OAuth App creds (b744/WS8) are deliberately NOT fetched here.
  # This cloud-init .env (/etc/rebar/.env) has no consumer of them; the containers
  # read the OAuth creds from infra/compose/.env (written by fetch-secrets.sh at
  # compose-up), and they are only required under auth.type = OAUTH. Adding them to
  # this unconditional CHANGEME-fail-fast map would make a fresh boot die on the
  # OAuth params before OAuth is even in use.
)

# "$${!PARAMS[@]}" renders to a real bash key expansion: one word PER KEY, not one word
# in total. (Writing the rendered form out here would itself be an unescaped
# interpolation -- see the note above the declaration.)
# ShellCheck sees the pre-render literal and wrongly reports a single-iteration loop.
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
