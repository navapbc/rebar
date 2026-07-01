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
  from the most recent good snapshot and re-provision. There is no hot standby —
  that is an accepted tradeoff for a single-team review gate.
- **RTO target: ≤ 2 hours** (provision + volume restore + Gerrit reindex). This is
  the **pass/fail criterion for the restore drill**: a drill (or a real recovery)
  that completes — snapshot → restored volume → verified-good Gerrit data — in
  **≤ 2 h** PASSES; longer than 2 h FAILS and triggers the DR back-out below.
  (Measured: the d251 drill ran **well under** target — see "Record the achieved
  RTO".)

## Rebuildable directories — EXCLUDE `index/` and `cache/` from restore archives

The Gerrit site has two **rebuildable** subdirectories that carry NO source of truth
and must be **excluded** from a logical restore archive (and discarded/regenerated on
restore), so the backup is smaller and a restore is faster and consistent:

- **`index/`** — the Lucene secondary index. It is derived entirely from NoteDb
  (`git/`); Gerrit rebuilds it with `gerrit.war reindex`. Restoring a stale index can
  even cause inconsistencies, so it is regenerated, never trusted from a backup.
- **`cache/`** — the H2 on-disk performance caches. Purely ephemeral; Gerrit
  repopulates them at runtime.

The **source of truth** to preserve is `git/` (NoteDb: repos, accounts, groups,
`refs/meta/config`), `db/`, and `etc/` (config + the SSH host key); `logs/` and
`data/replication` are operational.

**Logical restore archive (preferred for off-box/DR; excludes the rebuildables):**

```bash
# On the box — a lean, consistent archive that OMITS index/ and cache/.
# (Quiesce first — see the drill below — so git/ + db/ are consistent.)
tar -C /var/gerrit/site -czf /tmp/gerrit-site-backup.tgz \
    --exclude=index --exclude=cache \
    git db etc logs
```

**EBS-snapshot restores include `index/` + `cache/`** (a snapshot is whole-volume), so
after restoring from a snapshot you MUST treat them as rebuildable: delete `index/`
(and optionally `cache/`) on the restored site and run a reindex before/at first boot:

```bash
# After mounting/promoting the restored site (NOT during the read-only drill):
rm -rf /var/gerrit/site/index/* /var/gerrit/site/cache/*
docker exec compose-gerrit-1 java -jar /var/gerrit/bin/gerrit.war reindex -d /var/gerrit
```

The non-destructive drill below only READS the restored copy to confirm `git/` (the
source of truth) is intact; it does not trust the snapshot's `index/`.

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

# 0. RTO clock — capture the drill START timestamp (see "Record the achieved RTO").
DRILL_START=$(date -u +%s)

# 1. Take a QUIESCED, filesystem-consistent on-demand snapshot.
#    Briefly STOP Gerrit so nothing is mid-write when the snapshot point is taken
#    (alternatively put Gerrit read-only). This is a ~seconds gate pause — and it is
#    SAFE: merged code is already on the GitHub mirror (S5), so the only thing paused
#    is the review gate, not the source of truth. Restart Gerrit immediately once the
#    snapshot has been INITIATED (create-snapshot captures a point-in-time; the wait
#    can run after Gerrit is back up).
sudo docker compose -f infra/compose/docker-compose.yml stop gerrit   # ~seconds pause
SNAP=$(aws ec2 create-snapshot --region $REGION --volume-id $SRC_VOL \
  --description "restore-drill $(date -u +%FT%TZ)" \
  --query SnapshotId --output text)
sudo docker compose -f infra/compose/docker-compose.yml start gerrit  # gate back up
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

## Record the achieved RTO (every drill run)

Capture the END timestamp once the restored copy is **verified good** (the
`ls`/`rev-parse` checks above all pass), and compute the elapsed time against the
**≤ 2 h** target. Record the result in the drill ticket / log:

```bash
DRILL_END=$(date -u +%s)
ELAPSED=$(( DRILL_END - DRILL_START ))
printf 'restore-drill RTO: %dm %ds (target: <= 2h / 7200s) — %s\n' \
  $(( ELAPSED / 60 )) $(( ELAPSED % 60 )) \
  "$( [ "$ELAPSED" -le 7200 ] && echo PASS || echo FAIL )"
```

**d251 drill result (2026-06-30, QUIESCED):** comfortably under target. Gerrit was
briefly stopped (`docker compose stop gerrit`) for a filesystem-consistent snapshot
and restarted immediately after the snapshot was initiated (Gerrit healthy again on
restart); the throwaway volume was created from that snapshot, attached, mounted
read-only, and the Gerrit NoteDb repos (`All-Projects.git`, `All-Users.git`,
`rebar.git`) were verified present, then the throwaway volume + snapshot were
deleted. **Measured end-to-end achieved RTO: ~126 s (≈ 2 minutes) — far under the
≤ 2 h (7200 s) target.** The live `prevent_destroy` data volume was never touched.
The snapshot is the long pole only because the volume is provisioned-large but
lightly used; EBS snapshots are incremental and copy only used blocks.

## DR back-out (reversibility)

Restores can go wrong. Keep every step reversible:

- **Failed DRILL.** The drill already operates only on a THROWAWAY volume, so
  back-out is just the cleanup above: `detach` + `delete-volume` the
  `rebar-restore-drill` volume (and optionally `delete-snapshot` the on-demand
  drill snapshot). The live volume was never touched — nothing to undo.

- **Failed REAL restore.** When recovering for real (replacing the live data
  volume from a snapshot), **DETACH-BUT-RETAIN the original volume — never delete
  it.** Only after the restored volume is **verified good** in service do you
  consider releasing the old one. If the restore is botched:
  1. Detach the (bad) restored volume from the instance.
  2. **Re-attach the original** `vol-06fa2e77a9dd97527` at `/dev/sdf` and bring
     Gerrit back on it — you are back to the pre-restore state, fully reversible.
  3. Diagnose, then retry the restore from a different/earlier snapshot.
  4. **Only after** a restore is verified good (Gerrit healthy, reindex clean,
     review metadata intact) do you release the old volume — snapshot it once more
     for safety, then delete. Until that point the original volume is the
     irreplaceable fallback and stays retained, detached but intact.

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
