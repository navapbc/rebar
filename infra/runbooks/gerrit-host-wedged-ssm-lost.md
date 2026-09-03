# Runbook — the Gerrit host is wedged and SSM is `ConnectionLost` (shell-free recovery)

The rebar Gerrit box fills its **root** volume; at 100% the SSM agent dies along with
everything else, so **you cannot get a shell**. `aws ssm send-command` invocations either
fail outright or hang in `InProgress` forever, and `aws ssm start-session` will not connect.
Meanwhile EC2 instance **and** system status checks stay `ok` for the entire outage — the
hypervisor and networking are genuinely healthy — so the `rebar-gerrit-ec2-instance-check` /
`rebar-gerrit-ec2-system-check` alarms **structurally cannot** detect this class.

This runbook recovers the host **from the AWS control plane only**: snapshot, grow the root
volume, reboot, verify. No shell is required at any step. It is the shell-free sibling of the
disk-full recovery in `review-bot-ops.md` §"Disk full — snapshot-leak recovery", which assumes
you can still run commands on the box; when SSM is `ConnectionLost`, use this one instead.

| | |
|---|---|
| **Instance** | `i-00880b2c7f13527c5` |
| **Region** | `us-east-1` |
| **Account** | `896586841071` |
| **Root volume** | `vol-0270fcf13709cf472` (gp3; size is Terraform `root_volume_size_gb`) |
| **Elastic IP** | `eipalloc-0bfd9c30897fc366a` — the address survives a stop/start, so stop/start is also address-safe if reboot is not enough |

---

## READ FIRST — growing the volume is an UNWEDGE, not a fix

**Growing the root volume buys headroom and nothing else.** The working set is completely
unchanged by it: on 2026-09-02 the box held **29 GiB of content before the resize and 29 GiB
after**. Only the denominator moved — 97%+ used on 30 GiB became 48% used on 60 GiB. Whatever
produced 29 GiB on a 30 GiB disk will, on the same trajectory, produce 60 GiB on a 60 GiB disk
and wedge the host again in exactly the same way, with exactly the same undetectable signature.

Therefore:

- **Do NOT record "grew the root volume" as the resolution** of a disk-full incident. It is
  the mitigation that restores service; it is not a root cause and it is not a fix.
- **Every use of this runbook REQUIRES a follow-up reclaim ticket** — filed before you close
  the incident — that identifies and bounds the actual consumer. See "Follow up (mandatory)".
- The measurement that identifies the consumer **cannot be taken during the incident** (it
  hangs — see "Measure what actually filled the disk"). Take it once the host is idle, and
  put the numbers on the reclaim ticket.

---

## Confirm it is this failure and not something else

Growing a volume for an unrelated outage costs money and hides the real fault. Work this
checklist first; all of it is readable from the API with no shell.

1. **Status checks are `ok` — both of them.** If either is `impaired`, this is a host/hypervisor
   fault, not a full disk; stop here and follow the EC2 impaired-instance path instead.
   ```bash
   aws ec2 describe-instance-status --region us-east-1 --instance-ids i-00880b2c7f13527c5 \
     --query 'InstanceStatuses[].{Inst:InstanceStatus.Status,Sys:SystemStatus.Status}' --output table
   ```
2. **SSM says `ConnectionLost`.** A `ConnectionLost` agent with a *recent* last-ping is the
   signature: the box was healthy, then stopped being able to run anything. Note the ping time —
   it dates the wedge (2026-09-02: last ping **03:09:52 PDT**).
   ```bash
   aws ssm describe-instance-information --region us-east-1 \
     --filters "Key=InstanceIds,Values=i-00880b2c7f13527c5" \
     --query 'InstanceInformationList[].{Ping:PingStatus,Last:LastPingDateTime,Agent:AgentVersion}' --output table
   ```
3. **Host metrics STOP mid-series rather than crossing a threshold.** This is the tell.
   `rebar/host:root_disk_used_percent` climbed 87% → 94% and then simply ended;
   `Rebar/Gate:GerritReachable` ended at the *same* timestamp, because both are published by the
   same 5-minute `observability.sh` probe and the probe died with the disk. A metric that stops
   is a dead publisher, not a healthy host.
   ```bash
   aws cloudwatch get-metric-statistics --region us-east-1 \
     --namespace rebar/host --metric-name root_disk_used_percent --statistics Maximum \
     --period 300 --start-time "$(date -u -v-12H +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
     --query 'reverse(sort_by(Datapoints,&Timestamp))[:20].{T:Timestamp,Pct:Maximum}' --output table
   ```
   > `rebar-root-disk-pressure` is `treat_missing_data = "breaching"` precisely so a dying host
   > cannot clear its own alarm to OK. If that alarm is silent while the series is truncated,
   > check the SNS subscription before you trust the absence of a page.
4. **CPU is normal, not pinned.** 20–50% throughout on 2026-09-02 ⇒ the kernel never wedged and
   this is not a runaway-process outage. Also check burst credits before blaming throttling: the
   instance is `unlimited` burstable and held `CPUCreditBalance` ≈ 440 with
   `CPUSurplusCreditBalance` 0.0, which **rules CPU throttling out**.
5. **The console is alive.** A serial console with a recent uptime proves the kernel is running
   and the box is not hung — it just cannot write anything. On 2026-09-02 this showed uptime
   ~11.9 h, continuous Docker veth churn, **no OOM kills and no I/O errors**.
   ```bash
   aws ec2 get-console-output --region us-east-1 --instance-id i-00880b2c7f13527c5 --latest \
     --output text | tail -60
   ```
6. **TCP 443 is OPEN but HTTP returns 504, then times out.** The reverse proxy outlives its
   upstream, so black-box probes see *slow failure* rather than connection refusal — which is
   why `git push` reports a 504 first and then reports nothing at all. Connection **refused**
   points somewhere else (nginx down, security group, instance stopped).
   ```bash
   nc -vz -w 5 rebar.solutions.navateam.com 443
   curl -sS -o /dev/null -w '%{http_code}\n' --max-time 30 \
     https://rebar.solutions.navateam.com/config/server/version
   ```

**All six match ⇒ root disk full, SSM dead, recover with the steps below.** Any mismatch —
especially an `impaired` check, a pinned CPU, OOM kills in the console, or connection refused —
means a different fault; do not grow the volume.

---

## Act — snapshot, grow, reboot (no shell required)

1. **Snapshot the root volume FIRST.** This is the reversibility step and it costs nothing but
   a few seconds to issue; do it before any mutation. You do **not** have to wait for it to
   complete before continuing — EBS snapshots are point-in-time from the moment they are
   created — but record the id.
   ```bash
   SNAP=$(aws ec2 create-snapshot --region us-east-1 --volume-id vol-0270fcf13709cf472 \
     --description "pre-unwedge root-full $(date -u +%FT%TZ)" --query SnapshotId --output text)
   echo "rollback snapshot: $SNAP"     # 2026-09-02: snap-06f35ff3a20fe4a73
   ```
2. **Grow the root volume.** Double it (2026-09-02: 30 → 60 GiB).
   ```bash
   aws ec2 modify-volume --region us-east-1 --volume-id vol-0270fcf13709cf472 --size 60
   aws ec2 describe-volumes-modifications --region us-east-1 --volume-ids vol-0270fcf13709cf472 \
     --query 'VolumesModifications[].{State:ModificationState,Pct:Progress,Size:TargetSize}' --output table
   ```
   **Wait for `optimizing`, not `completed`.** `optimizing` means the new capacity is already
   available to the instance; `completed` only marks the end of background re-striping and can
   take hours. On 2026-09-02 `optimizing` was reached in **~16 seconds**.
3. **Reboot.** AL2023's cloud-init runs `growpart` at boot, so the **partition and the
   filesystem both expand with no shell** — you do not need to run `growpart`/`xfs_growfs`
   yourself, which is what makes this path viable when SSM is gone.
   ```bash
   aws ec2 reboot-instances --region us-east-1 --instance-ids i-00880b2c7f13527c5
   ```
   If the reboot does not take (the instance never stops responding), escalate to
   `stop-instances` + `start-instances`. The Elastic IP `eipalloc-0bfd9c30897fc366a` means the
   public address survives a stop/start, so that is address-safe — it is just more disruptive
   (a stop/start moves the instance to new hardware and takes longer), which is why reboot is
   the first attempt.

---

## Verify

Recovery is complete when all three are true. On 2026-09-02 this was reached at **07:10 PDT**.

```bash
# 1. Gerrit answers 200 (not 504, not a timeout)
curl -sS -o /dev/null -w '%{http_code}\n' https://rebar.solutions.navateam.com/config/server/version

# 2. SSM is Online again — you have a shell back
aws ssm describe-instance-information --region us-east-1 \
  --filters "Key=InstanceIds,Values=i-00880b2c7f13527c5" \
  --query 'InstanceInformationList[].PingStatus' --output text        # -> Online

# 3. The filesystem actually grew (now that SSM works)
aws ssm send-command --region us-east-1 --instance-ids i-00880b2c7f13527c5 \
  --document-name AWS-RunShellScript --parameters 'commands=["df -h /"]'
# 2026-09-02: /dev/nvme0n1p1  60G  29G  32G  48% /
```

Also confirm the 5-minute probe resumed publishing — `rebar/host:root_disk_used_percent` and
`Rebar/Gate:GerritReachable` should both have fresh datapoints, and `rebar-root-disk-pressure`
and `rebar-gerrit-gate-down` should return to OK. A truncated series that stays truncated means
the probe did not restart even though Gerrit did.

---

## Measure what actually filled the disk

**This step is BLOCKED during the incident and must be taken afterwards, on an idle host.**
On 2026-09-02 both `du -xh /` and `docker system df` **hung indefinitely** on the loaded box
while trivial commands (`df`, `lsblk`) returned instantly — the expensive walks are exactly the
ones a saturated host cannot complete. Do not try to force them mid-incident; unwedge first,
let the box settle, then measure. **This measurement is what identifies the actual consumer**,
and its numbers belong on the reclaim ticket.

```bash
aws ssm send-command --region us-east-1 --instance-ids i-00880b2c7f13527c5 \
  --document-name AWS-RunShellScript --parameters 'commands=[
    "du -xh --max-depth=2 / | sort -rh | head -30",
    "docker system df -v | head -40"
  ]'
```

**What the 2026-09-02 measurement found** (worked context for the next reader — expect the same
shape, and check whether these are still the consumers):

| Path | Size |
|---|---|
| root total | 28 G |
| `/var` | 26 G |
| `/var/lib/docker` | **17 G** — `overlay2` 16 G across **67** layer directories |
| `/var/tmp` | 3.6 G |
| `/var/log` | 1.8 G — of which the journal is 1.7 G |

The important part is the discrepancy: **Docker's own accounting claimed only ~9.5 GB, with
ZERO dangling images**, against 17 G actually on disk. Roughly **6.5 GB is orphaned `overlay2`
layer state that Docker does not know it owns** — so it is invisible to Docker's reclaim path.
`docker system prune` frees only **~1.06 GB**, about **3.5%** of the problem. That is why four
separate rounds of prune-based mitigation never held: the prune is not wrong, it is simply
addressing a twentieth of the growth. Any durable fix has to reach the orphaned layer state (or
stop producing it), not just re-run a prune more often.

---

## Follow up (mandatory)

1. **File the reclaim ticket before closing the incident.** It must carry the post-recovery
   measurement above and target the *consumer*, not the symptom. Link it to bug
   `3276-2f81-8c75-4ddd` (`cyclopean-bloodshot-hoatzin`) and to the bounded-ceiling epic
   `6202-e1c7-c57f-4897` (`endowed-upset-scaup`), which owns putting a real ceiling on this
   growth so the host cannot reach 100% again.
2. **Reconcile Terraform with the out-of-band resize.** `modify-volume` changes the live volume
   but not the code; `root_volume_size_gb` in `infra/terraform/variables.tf` must match, or the
   next `terraform plan` shows a spurious diff (the 30 → 60 bump is already codified — commit
   `3e22693efc90`, "infra: codify 60 GiB root volume"). If you grow again, bump the variable in
   the same change.
3. **Note the detection gap in the incident record.** The EC2 status-check alarms cannot see
   this class, and the disk-pressure alarm depends on a publisher that dies with the host. The
   only reliable external signal during the outage was the **truncated metric series** plus the
   black-box 504. Any detection work belongs on the bounded-ceiling epic.

## See also

- `infra/runbooks/review-bot-ops.md` §"Disk full — snapshot-leak recovery (incident 2731 /
  bug 9d7c)" — the **with-a-shell** disk-full path: snapshot-cache janitor, autodeploy prune
  gate, `REBAR_GATE_MIN_FREE_GIB` low-disk admission, and the `rebar-root-disk-pressure` alarm.
  Use that one whenever SSM is still `Online`.
- `infra/runbooks/provision-restore.md` — full rebuild + EBS restore, if the box cannot be
  recovered in place (and the RTO model: merged code is on the GitHub mirror and never at risk).
- `infra/runbooks/gerrit-upgrade.md` — the snapshot-first, rollback-ready posture this runbook
  borrows for step 1.
- `infra/terraform/monitoring_autodeploy.tf` — the `rebar-root-disk-pressure` alarm and the
  `rebar/host:root_disk_used_percent` metric contract; `infra/scripts/observability.sh` — the
  5-minute probe that publishes it (and that dies with the disk).
