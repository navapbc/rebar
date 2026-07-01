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
# 1) Resolve the data volume's NVMe device dynamically.
# ---------------------------------------------------------------------------
# Nitro/Graviton presents EBS volumes as /dev/nvme*n1, NOT the /dev/sdf we asked
# for in the attachment. We match by the EBS volume id. AWS encodes the volume
# id (minus dashes) in the NVMe controller serial number.
VOL_NODASH=$(echo "${data_volume_id}" | tr -d '-')

DATA_DEV=""
for d in /dev/nvme*n1; do
  [ -e "$d" ] || continue
  if nvme id-ctrl -v "$d" 2>/dev/null | grep -qi "$VOL_NODASH"; then
    DATA_DEV="$d"
    break
  fi
done

# Fallback: the by-id symlinks also embed the volume id in the serial.
if [ -z "$DATA_DEV" ]; then
  for link in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*; do
    [ -e "$link" ] || continue
    case "$link" in
      *"$VOL_NODASH"*)
        DATA_DEV=$(readlink -f "$link")
        break
        ;;
    esac
  done
fi

if [ -z "$DATA_DEV" ]; then
  echo "FATAL: could not resolve NVMe device for volume ${data_volume_id}" >&2
  exit 1
fi
echo "Resolved data volume ${data_volume_id} -> $DATA_DEV"

# ---------------------------------------------------------------------------
# 2) Format (idempotent) + mount at /var/gerrit, persisted in fstab by UUID.
# ---------------------------------------------------------------------------
if ! blkid "$DATA_DEV" >/dev/null 2>&1; then
  echo "No filesystem on $DATA_DEV — creating xfs"
  mkfs.xfs "$DATA_DEV"
fi

mkdir -p /var/gerrit

DATA_UUID=$(blkid -s UUID -o value "$DATA_DEV")
if ! grep -q "$DATA_UUID" /etc/fstab; then
  echo "UUID=$DATA_UUID /var/gerrit xfs defaults,nofail 0 2" >> /etc/fstab
fi
mount -a

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
declare -A PARAMS=(
  ["/rebar/prod/gerrit-admin-password"]="GERRIT_ADMIN_PASSWORD"
  ["/rebar/prod/gerrit-ssh-host-ed25519-key"]="GERRIT_SSH_HOST_ED25519_KEY"
  ["/rebar/prod/github-replication-deploy-key"]="GITHUB_REPLICATION_DEPLOY_KEY"
  ["/rebar/prod/mcp-hmac-signing-key"]="MCP_HMAC_SIGNING_KEY"
  ["/rebar/prod/anthropic-api-key"]="ANTHROPIC_API_KEY"
  ["/rebar/prod/alert-endpoint"]="ALERT_ENDPOINT"
  ["/rebar/prod/gerrit-bot-token"]="GERRIT_BOT_TOKEN"
)

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
