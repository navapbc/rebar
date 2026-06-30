# Runbook — provision from scratch & EBS restore drill (S7)

Authoritative procedure for **rebuilding the rebar Gerrit host** and for the
**non-destructive backup-restore drill**. Read the RTO model first — it decides
how hard you push.

## RTO model: freeze-and-restore, code is always safe

- **Merged code is never at risk.** Every change that merges through Gerrit is
  replicated to the `navapbc/rebar` GitHub mirror (S5, one-way door — ADR-0010).
  Losing the box does **not** lose merged history; GitHub holds it.
- **At risk on host loss:** in-flight (unmerged) changes, Gerrit NoteDb review
  metadata (votes/comments), and the box's local state — all of which live on the
  **data volume** `vol-06fa2e77a9dd97527` and are captured by the DLM daily
  snapshots (retain=7, see below).
- **Posture is freeze-and-restore, not HA.** A single box; on failure we restore
  from the most recent good snapshot and re-provision. The RTO target is **a few
  hours** (provision + volume restore + Gerrit reindex), not minutes. There is no
  hot standby — that is an accepted tradeoff for a single-team review gate.

## Snapshot identification (the backup of record)

S1 owns the backup: a **DLM lifecycle policy** (`infra/terraform/backup.tf`,
`aws_dlm_lifecycle_policy.gerrit_data`) takes a **daily** snapshot of the data
volume at 03:00, **tag-targeted** on `Name=rebar-gerrit-data`, and retains the
**7** most recent (`var.snapshot_retention_count`). DLM tags each snapshot
`SnapshotCreator=rebar-dlm` and copies the volume's tags.

> **S7 only MONITORS this.** Do NOT declare a second DLM policy in S7. `monitoring.tf`
> reads the volume via `data.aws_ebs_volume.data` and a `check` block asserts the
> snapshot target exists — it never manages the policy or the volume.

List the available restore points (newest first):

```bash
aws ec2 describe-snapshots --region us-east-1 --owner-ids self \
  --filters "Name=volume-id,Values=vol-06fa2e77a9dd97527" \
  --query 'reverse(sort_by(Snapshots,&StartTime))[].{Id:SnapshotId,Started:StartTime,State:State,Size:VolumeSize}' \
  --output table
```

Pick the newest `completed` snapshot whose `StartTime` predates the incident.

## Provision from scratch

Order matters — secrets before the instance, infra before compose.

1. **Bootstrap state backend** (only if the S3 state bucket/lock is gone):
   `cd infra/bootstrap && terraform init && terraform apply` — creates
   `rebar-tfstate-896586841071`. Normally already exists; skip.
2. **Populate secrets.** The SSM SecureString slots under `/rebar/prod/*`
   (`infra/terraform/ssm.tf`) are placeholders (`CHANGEME`). Set the real values
   BEFORE the apply — `user_data.sh` fails fast on a `CHANGEME` sentinel:
   ```bash
   aws ssm put-parameter --overwrite --type SecureString \
     --name /rebar/prod/alert-endpoint --value 'ops@example.com'
   # …repeat for gerrit-admin-password, github-replication-deploy-key,
   #   mcp-hmac-signing-key, anthropic-api-key, gerrit-bot-token, ssh host key.
   ```
3. **Apply infra** (instance, volume, EIP, DLM, IAM, monitoring):
   ```bash
   cd infra/terraform
   terraform init                       # S3 remote backend, state lock
   terraform plan -out plan.tfplan      # REVIEW before apply — see "plan-before-apply"
   terraform apply plan.tfplan
   ```
   Then **confirm the SNS email subscription** (a one-time click in the alert
   inbox — `aws_sns_topic_subscription.alerts_email` is created PendingConfirmation).
4. **Bring services up on the box** (over SSM Session Manager — no inbound SSH):
   - `infra/scripts/fetch-secrets.sh` — pull SSM secrets to disk.
   - `infra/scripts/compose-up.sh` — start the Gerrit + review-bot + nginx stack.
   - `infra/scripts/bootstrap-gerrit-admin.sh` — seed the admin account.
   - the gerrit/replication setup scripts in `infra/gerrit/` — install the
     replication deploy key + plugin config so the GitHub mirror resumes.
   - `infra/scripts/install-observability.sh` — re-arm the 5-min probe timer.
   - `infra/scripts/install-certbot-timer.sh` — TLS renewal.
5. **Restore data** if this is a recovery (not a clean build): follow the restore
   drill's create-volume path, but this time attach the restored volume as the
   real `/var/gerrit` data volume (and update the terraform state / volume id
   accordingly) rather than to a temp mountpoint.

## Non-destructive EBS restore drill (rehearse the recovery)

Goal: **prove a snapshot actually restores** without ever touching the live
`prevent_destroy` data volume. Everything here operates on a THROWAWAY copy.

```bash
REGION=us-east-1
INSTANCE_ID=i-00880b2c7f13527c5
SRC_VOL=vol-06fa2e77a9dd97527

# 1. Take an on-demand snapshot (or reuse the newest DLM snapshot above).
SNAP=$(aws ec2 create-snapshot --region $REGION --volume-id $SRC_VOL \
  --description "restore-drill $(date -u +%FT%TZ)" \
  --query SnapshotId --output text)
aws ec2 wait snapshot-completed --region $REGION --snapshot-ids $SNAP

# 2. Find the instance's AZ and create a THROWAWAY volume from the snapshot
#    in that same AZ (EBS attach requires same-AZ).
AZ=$(aws ec2 describe-instances --region $REGION --instance-ids $INSTANCE_ID \
  --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' --output text)
DRILL_VOL=$(aws ec2 create-volume --region $REGION --snapshot-id $SNAP \
  --availability-zone $AZ --volume-type gp3 \
  --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=rebar-restore-drill},{Key=Project,Value=rebar}]' \
  --query VolumeId --output text)
aws ec2 wait volume-available --region $REGION --volume-ids $DRILL_VOL

# 3. Attach the THROWAWAY volume (use a free device, NOT /dev/sdf which the live
#    data volume uses). On Nitro/Graviton it surfaces as an NVMe device.
aws ec2 attach-volume --region $REGION --volume-id $DRILL_VOL \
  --instance-id $INSTANCE_ID --device /dev/sdg
aws ec2 wait volume-in-use --region $REGION --volume-ids $DRILL_VOL
```

Then, on the box (SSM Session Manager), **mount read-only at a temp path and
verify the Gerrit data is intact** — do NOT mount over `/var/gerrit`:

```bash
# Identify the just-attached NVMe device (the one NOT already mounted at /var/gerrit).
lsblk -o NAME,SIZE,MOUNTPOINT
sudo mkdir -p /mnt/restore-drill
sudo mount -o ro,nouuid /dev/nvme2n1 /mnt/restore-drill   # adjust device

# VERIFY: the Gerrit site/repos/NoteDb are present on the restored copy.
ls /mnt/restore-drill/site/git/                 # bare repos incl. All-Projects.git, All-Users.git
ls /mnt/restore-drill/site/git/All-Users.git/   # NoteDb (review metadata) lives in All-Users
test -f /mnt/restore-drill/site/etc/gerrit.config && echo "gerrit.config present"
sudo git -C /mnt/restore-drill/site/git/All-Projects.git rev-parse --is-bare-repository
```

Cleanup — **leave the original volume untouched**:

```bash
sudo umount /mnt/restore-drill                                   # on the box
aws ec2 detach-volume --region $REGION --volume-id $DRILL_VOL    # from your shell
aws ec2 wait volume-available --region $REGION --volume-ids $DRILL_VOL
aws ec2 delete-volume  --region $REGION --volume-id $DRILL_VOL
# Optionally delete the on-demand drill snapshot if you created one:
aws ec2 delete-snapshot --region $REGION --snapshot-id $SNAP
```

The live `prevent_destroy` volume `vol-06fa2e77a9dd97527` is **never** detached,
unmounted, or deleted by this drill.

## `prevent_destroy` removal procedure (intentional volume replace)

The data volume and the EIP carry `lifecycle { prevent_destroy = true }`
(`main.tf`) so a stray `terraform destroy` / instance replacement can't take the
Gerrit data. To **deliberately** replace the volume (e.g. resize beyond a live
grow, or swap in a restored volume):

1. **Snapshot first** (always have a restore point — see the drill).
2. Temporarily comment out / set `prevent_destroy = false` on
   `aws_ebs_volume.data` in `main.tf`. Keep this change LOCAL; do not merge it.
3. `terraform plan -out plan.tfplan` and read the plan — confirm it will replace
   ONLY the intended volume, nothing else.
4. `terraform apply plan.tfplan`.
5. **Immediately restore `prevent_destroy = true`** and apply again so the guard
   is back on. Never leave the guard off on `main`.

## plan-before-apply on the shared remote backend

State lives in the S3 backend `rebar-tfstate-896586841071`
(`infra/terraform/versions.tf`) with **S3-native lock** (`use_lockfile`, requires
Terraform >= 1.10 — an older CLI silently runs with NO locking). Always:

1. `terraform init` against the real backend (no `-backend=false`).
2. `terraform plan -out plan.tfplan` — and **read it**. For S7, confirm the plan
   does NOT propose destroying/replacing S1-owned resources (instance, data
   volume, DLM, IAM roles); S7 should only add the SNS topic/subscription and the
   alarms, and read the data sources.
3. `terraform apply plan.tfplan` (apply the saved plan, not a fresh one, so what
   you reviewed is exactly what runs).
4. Never run two applies concurrently — the lock serializes them; if you see a
   lock error, someone else is applying. Wait, don't `-lock=false`.
