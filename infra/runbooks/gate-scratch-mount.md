# Runbook — put the gate-scratch EBS volume into service on a RUNNING host

**Ticket:** `renowned-corked-hapuku` (`dcc3-75ee-26ce-4840`). **Audience:** an operator with SSM
or SSH root on `i-00880b2c7f13527c5`. **Do not skip the quiesce step**; the reason is in §2.

This is the post-boot counterpart to `infra/terraform/user_data.sh`. That script does the same
job at LAUNCH and is not usable here: cloud-init runs it once, on first boot, and this volume was
attached to an already-running instance. Every command below was chosen against the host's
CURRENT state, verified read-only on 2026-09-05 22:24 UTC, not against what `user_data.sh` does.

## 0. State this procedure assumes (re-verify before starting — 30 seconds)

```
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,SERIAL
blkid /dev/nvme2n1 || echo "NO FILESYSTEM"
grep gate-scratch /etc/fstab || echo "no fstab line"
mountpoint /var/lib/rebar/gate-scratch
ls -la /var/lib/rebar/.gate-scratch-required /var/lib/rebar/gate-scratch/.gate-scratch-mounted
df -h /
```

Expected, and what was measured:

| check | expected |
|---|---|
| `nvme2n1` | 50G, **no FSTYPE**, **no MOUNTPOINT**, SERIAL `vol06780b8557d1416b7` |
| `blkid /dev/nvme2n1` | `NO FILESYSTEM` |
| fstab | `no fstab line` |
| `mountpoint` | `is not a mountpoint` |
| both markers | `No such file or directory` |
| `df -h /` | **49G used of 60G, 81%** |

**If `blkid` reports a filesystem, STOP.** Step 4 formats. A filesystem there means someone
else acted, or the device letter moved; re-identify by SERIAL before touching anything.

> **Never carry a device name over from an earlier note — this is demonstrated, not theoretical.**
> During a host reboot on 2026-09-05 at ~23:52 UTC, `df -h` reported `/dev/nvme2n1` mounted at
> `/var/gerrit`; one minute later `lsblk` reported `nvme1n1` at `/var/gerrit` and `nvme2n1` as the
> unmounted gate-scratch volume. The two reads DISAGREED about which node backs Gerrit's data
> volume, on the same host, a minute apart — most likely NVMe enumeration still settling.
> `/var/gerrit` is mounted by UUID so nothing was harmed. But step 4 runs `mkfs.xfs`, and an
> operator who had pasted `/dev/nvme2n1` from an earlier note into that command during the window
> would have **formatted Gerrit's data volume.** Re-run the SERIAL check in this section
> immediately before step 4, every time; the `blkid` interlock in step 4 is the second net.

> **The disk figure has moved and it matters.** The ticket recorded 34G / 57%. It is now
> **49G / 81% — 12G free**. Root has gained ~15G in about eleven hours while gate scratch ran on
> it. That is no longer a comfortable degraded state: the volume this procedure mounts is the
> thing keeping Gerrit's root filesystem from filling, and a full root takes Gerrit down.

## 1. Expected duration and blast radius

| phase | time | what is broken meanwhile |
|---|---|---|
| quiesce (§2) | ~1 min | review-bot stops voting `LLM-Review`; an in-flight review dies |
| format + fstab + mount (§3-5) | ~1 min | nothing further |
| markers (§6) | seconds | nothing |
| restart review-bot (§7) | ~1 min | end of the outage window |
| reclaim (§8) | **minutes to tens of minutes** | nothing — the bot is already back up |

**Review-bot downtime is roughly 3-5 minutes** (§2 through §7). §8 runs afterwards, with the bot
serving. Do not run this while a change you need is waiting on `LLM-Review`; the vote will simply
be re-requested afterwards.

> Budget generously for §8. A read-only `du -sh` over this scratch directory was started at
> 22:30 UTC and had **not finished after 25 minutes**, so it was cancelled. Root is IOPS-starved
> and holds 203 snapshot directories. **This runbook therefore never depends on `du`** — every
> space check below uses `df`, which is O(1).

## 2. Quiesce the writers — and why "just mount it" is WRONG

`/var/lib/rebar/gate-scratch` is being written right now (a `reviewbot-*` clone had an mtime
inside the last two minutes at inspection). There are two independent reasons a live mount is
unsafe, and the second one is the one that is easy to miss:

1. **Shadowing.** Mounting over a non-empty directory hides its contents. Processes holding open
   file descriptors keep writing to the now-invisible inodes on ROOT, so the space is never
   reclaimed while they live, and it is invisible to `du`. That is very likely a contributor to
   the unattributed ~21 GB on `cyclopean-bloodshot-hoatzin`.
2. **The container would not see the mount at all.** `infra/compose/docker-compose.yml:305` binds
   **`/var/lib/rebar` — the PARENT** — into `review-bot`, not the mount point. Docker bind mounts
   default to `rprivate` propagation, so a mount created on the host UNDERNEATH an existing bind
   does **not** appear inside the running container. The bot would keep writing to the old root
   directory through its existing bind while the host showed a correctly mounted, empty volume —
   a split brain in which every check you run says "fixed" and nothing actually moved.

   **This is why §7 is a mandatory recreate, not an optional tidy-up.**

There is no safe no-quiesce variant. Do this:

```
cd /var/gerrit/compose 2>/dev/null || cd "$(dirname "$(find / -name docker-compose.yml -path '*compose*' 2>/dev/null | head -1)")"
docker compose stop review-bot
```

**Confirm quiescence — do not assume it:**

```
docker compose ps review-bot                      # expect: exited / not running
fuser -vm /var/lib/rebar/gate-scratch 2>&1        # expect: no processes listed
ls -la --time-style=full-iso /var/lib/rebar/gate-scratch   # note the newest mtime
sleep 30
ls -la --time-style=full-iso /var/lib/rebar/gate-scratch   # newest mtime MUST be unchanged
```

If `fuser` still lists PIDs, identify them (`ps -o pid=,command= -p <pid>`) before continuing.
A host-side `rebar` gate run is the other likely holder.

**Rollback for §2:** `docker compose start review-bot`. Nothing has been changed on disk.

## 3. Move the existing contents ASIDE — do not mount over them

This replaces the ticket's "mount, then reclaim later" ordering, and it is strictly better: a
rename on the same filesystem is atomic and instant, it leaves the mount point empty so nothing
is ever shadowed, and it makes the rollback a second rename rather than a data recovery.

```
mv /var/lib/rebar/gate-scratch /var/lib/rebar/gate-scratch.stranded
mkdir -p /var/lib/rebar/gate-scratch
```

**Verify:** `ls -A /var/lib/rebar/gate-scratch` prints nothing; `ls /var/lib/rebar/` shows both.

**Rollback:** `rmdir /var/lib/rebar/gate-scratch && mv /var/lib/rebar/gate-scratch.stranded
/var/lib/rebar/gate-scratch`. Fully reversible; no data has been deleted.

## 4. Format the volume

Re-confirm the device by SERIAL, then format. `blkid` returning nothing is the safety interlock.

```
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,SERIAL /dev/nvme2n1     # SERIAL must be vol06780b8557d1416b7
blkid /dev/nvme2n1 && { echo "REFUSING: filesystem present"; exit 1; }
mkfs.xfs /dev/nvme2n1
blkid /dev/nvme2n1                                            # now prints TYPE="xfs" and a UUID
```

**Verify:** `blkid` prints `UUID="..." TYPE="xfs"`. Record that UUID.

**Rollback:** none is needed — the volume held no data. If you must undo, the volume can be
re-formatted or replaced; nothing on it is referenced yet.

## 5. fstab by UUID, then mount

By UUID, never by `/dev/nvme2n1`: NVMe enumeration order is not stable across reboots.
`nofail` so a missing volume degrades to an unmounted directory rather than a boot failure —
matching what `user_data.sh` writes, so the two paths converge on the same fstab line.

```
cp -p /etc/fstab /etc/fstab.pre-gate-scratch
UUID=$(blkid -s UUID -o value /dev/nvme2n1)
grep -q " /var/lib/rebar/gate-scratch " /etc/fstab || \
  echo "UUID=$UUID /var/lib/rebar/gate-scratch xfs defaults,nofail 0 2" >> /etc/fstab
mount -a
```

**Verify — this is the hard gate; do not proceed past a failure:**

```
mountpoint -q /var/lib/rebar/gate-scratch && echo MOUNTED || { echo "NOT MOUNTED"; exit 1; }
lsblk -o NAME,FSTYPE,MOUNTPOINT /dev/nvme2n1   # xfs  /var/lib/rebar/gate-scratch
df -h /var/lib/rebar/gate-scratch              # ~50G, nearly empty
grep gate-scratch /etc/fstab                   # EXACTLY ONE line
findmnt --verify --verbose                     # fstab is parseable
```

**Rollback:** `umount /var/lib/rebar/gate-scratch; cp -p /etc/fstab.pre-gate-scratch /etc/fstab`.

## 6. The markers — order, and why this order

The two markers live on **different filesystems on purpose**, and that is the whole mechanism:

- `.gate-scratch-required` sits on **ROOT**, beside the mount point, so it SURVIVES an unmount —
  it is the standing *declaration* that this host is supposed to have a scratch volume.
- `.gate-scratch-mounted` sits **INSIDE** the mount, on the **VOLUME**, so it DISAPPEARS with it —
  it is the *proof*.

Declaration present + proof absent ⇒ gate admission refuses instead of silently repopulating
root. Declaration absent ⇒ admission assumes there is no volume and proceeds on root. That
third state is the one this host has been in, which is why nothing complained.

```
chmod 0700 /var/lib/rebar/gate-scratch
touch /var/lib/rebar/gate-scratch/.gate-scratch-mounted     # PROOF first (on the volume)
touch /var/lib/rebar/.gate-scratch-required                 # DECLARATION second (on root)
```

**Proof before declaration, and `chmod` before both.** Creating the declaration first opens a
window in which admission refuses every gate on the box; creating the proof first has no bad
window at all, because proof-without-declaration is exactly the state the host is in today.
This DIVERGES from `user_data.sh`'s declaration-then-proof order, deliberately: that script runs
pre-service at boot, when no gate can be admitted, so its window is empty. Here it is not.
`chmod` first so a marker is never written into a world-readable directory (bug `ad8d`, AC3).

**Verify they are on DIFFERENT devices — the property, not just the filenames:**

```
stat -c '%n %D' /var/lib/rebar/.gate-scratch-required \
                /var/lib/rebar/gate-scratch/.gate-scratch-mounted
```
The two `%D` device ids MUST DIFFER. If they match, the mount is not in place and you have just
written both markers onto root — remove both and return to §5.

```
ls -ld /var/lib/rebar/gate-scratch      # drwx------
```

**Rollback:** `rm -f /var/lib/rebar/.gate-scratch-required
/var/lib/rebar/gate-scratch/.gate-scratch-mounted`. Remove the DECLARATION first when rolling
back, for the same reason it is written last.

## 7. Recreate the review-bot — MANDATORY, see §2 reason 2

`start` is not sufficient in principle: the container must re-establish its bind so the new mount
is visible inside it. Recreate:

```
docker compose up -d --force-recreate review-bot
```

**Verify the container sees the VOLUME, not root:**

```
docker compose exec review-bot sh -c 'stat -c %D /var/lib/rebar/gate-scratch; stat -c %D /var/lib/rebar'
```
The two device ids MUST DIFFER inside the container. Then:

```
docker compose exec review-bot sh -c 'ls -a /var/lib/rebar/gate-scratch'   # .gate-scratch-mounted present
docker compose logs --tail=50 review-bot                                   # no mount/permission errors
```

**Rollback:** if the bot misbehaves, `docker compose stop review-bot`, then §6 rollback, §5
rollback, then §3 rollback restores the original directory, then `docker compose up -d
--force-recreate review-bot`.

## 8. Reclaim the stranded copy — the POINT OF NO RETURN

Everything up to here is reversible. **This step is not.** Do it only after §7 verifies clean,
and only once you are content the bot is healthy (a completed review is the strongest signal).

```
df -h /                                        # record "before"
rm -rf /var/lib/rebar/gate-scratch.stranded
df -h /                                        # expect used to drop by the stranded size
```

The contents are genuinely scratch — review clones, gate snapshots, semgrep state, temp files —
so there is nothing to migrate. If you want a pause, leave the directory in place and delete it
in a later window; it costs only the space it already occupies.

**Verify:** `df -h /` shows a materially lower `Use%`. `ls /var/lib/rebar/` no longer lists
`gate-scratch.stranded`.

## 9. Confirm from CloudWatch, not from the host

The host telling you it is mounted is the same host that has been wrong for days. Confirm from
outside, after one metric period:

```
aws cloudwatch get-metric-statistics --namespace rebar/host \
  --metric-name gate_scratch_mounted --start-time "$(date -u -d '15 min ago' +%FT%TZ)" \
  --end-time "$(date -u +%FT%TZ)" --period 300 --statistics Maximum
aws cloudwatch describe-alarms \
  --alarm-names rebar-gate-scratch-unmounted rebar-gate-scratch-disk-high \
  --query 'MetricAlarms[].{Name:AlarmName,State:StateValue}' --output table
```

- `gate_scratch_mounted` must publish **1**.
- `disk_used_percent` for the gate-scratch mount must START PUBLISHING. Its absence is what has
  been holding `rebar-gate-scratch-disk-high` in ALARM via `treat_missing_data = "breaching"` —
  that alarm is dead-man-correct and its return to OK is the real proof, not the host's `df`.
- Both alarms must read **OK**.

If `gate_scratch_mounted` still reads 0, the metric publisher is `infra/scripts/observability.sh`
(`GATE_SCRATCH_MOUNT` default at `:72`, probe at `:233`, dimension at `:238`) — check it is
running before assuming the mount failed.

## 10. Prove it from Terraform's side too — and after every future apply

Mounting this volume repairs THIS host. The reason it went unnoticed for days is structural:
`user_data.sh` is the only thing that formats and mounts a volume, and cloud-init runs it once,
at first boot. Terraform knew the volume was attached; nothing compared that against whether the
host was actually using it.

`scripts/assert_volumes_in_service.py` performs that join. Run it after this procedure, and
after ANY future `terraform apply` that adds or replaces a volume:

```
cd infra/terraform && terraform show -json > /tmp/tf.json
python scripts/assert_volumes_in_service.py \
  --plan-json /tmp/tf.json --instance-id i-00880b2c7f13527c5
```

Expected once §5 has succeeded: every attached volume listed `in-service`, exit 0. Before this
procedure it exits 1 and names `vol06780b8557d1416b7` as `NOT-IN-SERVICE`. A volume the host does
not report at all is `UNKNOWN`, which also fails — "I could not tell" must not read as "fine".

## 11. What is still NOT closed

Auto-*mounting* an attached volume post-boot would be safe; auto-*formatting* one is not, and
this volume was raw, so no reconciler can cover the case end to end without risking a restored
backup. Detection is therefore the ceiling, and §10 is that detection. Two residual items:

- rebar's own gate admission still treats "no declaration marker" as "this host has no scratch
  volume" and proceeds on root (`rebar.llm.gate_admission.scratch_unavailable_detail` returns
  `None`). That is deliberate — the markers are opt-in by provisioning so no laptop or CI runner
  changes behaviour on upgrade — but it is why THIS host was silent. Deriving the declaration
  from something that survives a post-launch attach is a real design change, and landing it
  while a volume is unmounted would refuse every gate on the box, so it needs its own ticket and
  must land AFTER this runbook has been executed.
- The expected mount set is still written per-volume in three places (`user_data.sh`,
  `infra/scripts/check-mounts.sh`, `infra/scripts/observability.sh`). A third volume gets no
  metric and no alarm until someone adds them by hand.
