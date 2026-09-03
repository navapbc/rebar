# Runbook — `/var/gerrit` DATA volume: where evidence goes, and how to reclaim it

The Gerrit **data** volume is not the root volume, and confusing them is the fastest way to
turn a headroom problem into an outage. Everything Gerrit cannot be rebuilt without lives
here: the git repositories, `All-Projects`, and the review database.

| | |
|---|---|
| **This volume** | `vol-06fa2e77a9dd97527` — gp3, mounted at **`/var/gerrit`** |
| **NOT this volume** | `vol-0270fcf13709cf472` — the **root** volume, `/` |
| **Instance / region** | `i-00880b2c7f13527c5` / `us-east-1` |
| **Capacity alarm** | `rebar-gerrit-data-disk-high` — `rebar/host:disk_used_percent` ≥ 85% |
| **Debris alarm** | `rebar-gerrit-data-disk-debris` — `rebar/host:data_disk_debris_bytes` ≥ 1 GiB |
| **Publisher** | `infra/scripts/observability.sh` §2 and §2c, 5-minute timer |

**Wrong runbook?**

- The **root** volume filled and you still have a shell → `review-bot-ops.md`
  §"Disk full — snapshot-leak recovery" and §"Disk-full triage (root-volume exhaustion)".
- The **root** volume filled and SSM is `ConnectionLost` → [`gerrit-host-wedged-ssm-lost.md`](gerrit-host-wedged-ssm-lost.md).
- Root-volume accumulator caps (docker, journald, `/var/tmp`) and the dedicated gate-scratch
  EBS volume are a different epic; do not implement them from here.

---

## READ FIRST — investigation evidence does not live on this volume

On **2026-08-26**, `/var/gerrit/rebar-quiet-window-evidence/` accumulated two
`epoch-probe-20260826T*` dumps of ~5.2G each. That **~11G was 65% of the volume's used
space** — one-off investigation output, sitting on the volume that holds every git repo,
written by ad-hoc operator/agent shell rather than by any rebar process. It was reclaimed
with operator approval on 2026-08-30, taking `/var/gerrit` from **37% to 15%**.

Nothing about that was visible to monitoring at the time. `disk_used_percent` reports how
full the volume is; it structurally cannot say *what* it is full of, so 11G of scratch read
exactly like 11G of legitimate repository growth.

### The policy (ADVISORY — nothing enforces it)

**Investigation, probe, and profiling output goes to the operator's workstation, not to the
box.** In order of preference:

1. **Off the box.** Stream the probe's output back through the SSM invocation and keep it
   locally, or `aws s3 cp` it to a bucket. `aws ssm send-command` already returns
   `StandardOutputContent`; a `du`/`find`/`git count-objects` census is kilobytes, not
   gigabytes, and never needs to touch a disk on the host.
2. **Root volume `/var/tmp`, bounded at spawn, if it truly must be staged on-box.** Use a
   ticket-named directory — `/var/tmp/rebar-evidence/<ticket-id>-<UTC-stamp>/` — and delete
   it in the same command that creates it (`trap 'rm -rf "$d"' EXIT`), so an interrupted
   session cannot leave it behind. This is the same bound-at-spawn rule `AGENTS.md` states
   for background processes, applied to bytes instead of CPU.
3. **Never `/var/gerrit`.** Not `/var/gerrit/evidence`, not
   `/var/gerrit/site/<anything-not-Gerrit's>`. If a specific investigation genuinely needs
   the data volume — because it is measuring the data volume — get explicit operator
   approval, put the size bound and the deletion deadline on the ticket first, and delete it
   the same day.

**This policy is advisory and cannot be made otherwise from inside this repository.** The
debris was written by a human/agent shell session on the host; no rebar code path was
involved, so there is no rebar code in which to place a guard. What *is* enforceable is
**detection**, below.

### The guard (ENFORCEABLE — detection, not prevention)

`observability.sh` §2c censuses every top-level entry under `/var/gerrit` that is not `site`
or `lost+found`, sums their bytes, and publishes `rebar/host:data_disk_debris_bytes` on each
5-minute tick. A clean volume publishes `0`. `rebar-gerrit-data-disk-debris`
(`infra/terraform/monitoring.tf`) pages at 1 GiB sustained.

What this does and does not buy:

- It **cannot prevent** the write. A shell on the box can create any path it likes.
- It **does** guarantee the write stops being silent: debris is named in journald
  (`journalctl -t rebar-health | grep debris_bytes`) within one probe interval, and pages
  once it passes 1 GiB — long before the 85% capacity alarm would notice.
- It is host-level (a shell script plus a CloudWatch alarm), so it works with any CI
  provider or none.

---

## Diagnose

Confirm the volume, then find the consumer. All of this needs a shell; if SSM is
`ConnectionLost` this is a root-volume wedge, not this runbook.

```bash
# 1. Which volume is actually pressured? /var/gerrit and / are separate filesystems.
df -h /var/gerrit /

# 2. What did the census already see? (5-minute cadence, so this is near-live.)
journalctl -t rebar-health --no-pager | grep -E 'debris_bytes|/var/gerrit used_percent' | tail -20

# 3. Top-level split: site/ (legitimate) vs everything else (debris by definition).
du -sh /var/gerrit/* 2>/dev/null | sort -h

# 4. Inside site/, the legitimate consumers, largest first.
du -sh /var/gerrit/site/* 2>/dev/null | sort -h | tail -10
du -sh /var/gerrit/site/git/* 2>/dev/null | sort -h | tail -10
```

Read the result against these expectations:

| Where the bytes are | Reading |
|---|---|
| Outside `site/` | **Debris.** Reclaimable — go to "Reclaim", below. |
| `site/git/*` | Repository growth. Real data; see "When it is genuinely `site/`". |
| `site/logs`, `site/cache`, `site/tmp` | Gerrit's own rotation/cache; bounded by Gerrit config, not by deletion. |
| `site/mcp-tickets`, `site/mcp-code` | The on-box MCP store and checkout (ADR 0104). Real state — do not delete. |
| `site/reviewbot` | The review-bot dedup DB (`voted.db`). Small, and deleting it re-votes changes. |

## Reclaim

**Non-`site/` debris is the only thing this runbook deletes.** Everything under `site/` is
live Gerrit state.

```bash
# Look before you delete — print the exact paths and sizes you are about to remove.
find /var/gerrit -mindepth 1 -maxdepth 1 -not -name site -not -name 'lost+found' \
  -exec du -sh {} +

# Remove them individually, by name. Never `rm -rf /var/gerrit/*` — that includes site/.
rm -rf /var/gerrit/<the-exact-path-you-just-printed>

# Confirm: usage drops, and the census publishes 0 on the next tick (<= 5 minutes).
df -h /var/gerrit
journalctl -t rebar-health --no-pager | grep debris_bytes | tail -3
```

Gerrit does **not** need restarting: nothing under `site/` was touched, so no open handle
was invalidated. `rebar-gerrit-data-disk-debris` returns to OK within ~15 minutes
(3 × 5-minute periods).

### When it is genuinely `site/`

If the census reads `0` and the volume is still pressured, this runbook is finished and the
growth is real. Do **not** delete inside `site/` to buy headroom. Instead:

- `git gc` / repack the largest repositories under `site/git/` (measure first with
  `git count-objects -vH`), and
- file a ticket against the growth driver. Tickets-branch enrichment fan-out and the
  blob-inclusive store are tracked separately (`4520`/`536b`/`0754`) — do not re-diagnose
  them here, and
- grow the volume only as an unwedge, with a follow-up reclaim ticket, exactly as
  [`gerrit-host-wedged-ssm-lost.md`](gerrit-host-wedged-ssm-lost.md) requires for the root
  volume. Growing a volume is never a resolution.

## Verify the guard still works

The census is exercised by `tests/scripts/test_observability_data_disk_debris.py`, which
drives the real `observability.sh` over a synthetic mount point. Run it after any change to
`observability.sh` §2 or §2c:

```bash
env PATH="$PWD/.venv/bin:$PATH" pytest -q tests/scripts/test_observability_data_disk_debris.py
env PATH="$PWD/.venv/bin:$PATH" pytest -q tests/unit/test_alarm_actions_terraform.py
```
