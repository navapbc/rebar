# CloudWatch alarm windows vs. the observability probe's publish cadence

Ticket `a9d1-c7f3-cfd9-44ff` (`illbred-sour-alpaca`). This is the reference the per-alarm
comments in `infra/terraform/monitoring*.tf` point at, so the arithmetic is stated once.

## The defect this replaces

Twenty-three `rebar/host` and `Rebar/Gate` alarms shipped the same shape:

    period = 300   evaluation_periods = 3   datapoints_to_alarm = 2   treat_missing_data = "breaching"

With `breaching`, an empty period is a breaching datapoint. `datapoints_to_alarm = 2` of
`evaluation_periods = 3` therefore means **two empty buckets alarm on their own**, with no
reading of any kind. Measured on 2026-09-05: 19 alarms in ALARM, ~17 of them false — the
metrics were publishing healthy values throughout.

The same shape has a second, worse failure. When the publisher is slower than the window, every
evaluation contains at least two empty buckets, so the alarm **re-arms itself forever**:
`rebar-docker-buildkit-cache-high` sat in ALARM for 10.5 hours while the underlying cache went
from 9% over budget to 0, and noticed neither. No value the metric could publish cleared it.

## The publisher's actual cadence contract

`infra/scripts/install-observability.sh` installs the probe as:

    [Service] Type=oneshot   TimeoutStartSec=240
    [Timer]   OnUnitActiveSec=5min

`OnUnitActiveSec` is measured from the last **completed** activation, so the interval between
publishes is `5 min + run duration`, and the run duration is bounded only by the 240 s start
timeout. **The contractual inter-arrival bound is therefore 5–9 minutes, not 5.** A 5-minute
CloudWatch bucket cannot be kept full by a publisher whose interval can reach 9 minutes: a
9-minute gap empties a bucket whenever it straddles one, so empty buckets are *guaranteed*, not
unlucky.

### Measured, first-hand, 2026-09-05 11:27Z-19:27Z

An 8-hour `get-metric-statistics` sweep at `period = 300` over every alarmed metric — 96 buckets
per metric, wide enough that absence is meaningful. (CloudWatch returns timestamps with a
`-07:00` offset; the namespace is lowercase `rebar/host`. Both are easy to get wrong and produce
a false "no data".)

| observability.sh line | metric | buckets present | max gap |
|---|---|---|---|
| 112 | `GerritReachable` | 93% | 10 min |
| 161 | `mcp_healthy` | 93% | 10 min |
| 169 | `disk_used_percent` (`/var/gerrit`) | 93% | 10 min |
| 192 | `root_disk_used_percent` | 93% | 10 min |
| 213 | `gate_scratch_mounted` | 93% | 10 min |
| 601 | `docker_storage_used_percent` | 70% | 35 min |
| 622 | `docker_buildkit_cache_used_percent` | 31% | 60 min |
| 640 | `docker_unaccounted_bytes` | 31% | 60 min |
| 696 / 713 | `journal_cap_in_effect` / `journal_used_percent` | 69% | 40 min |
| 771 | `var_tmp_cleanup_active` | 69% | 40 min |
| 798 | `var_tmp_used_percent` | **0%** | — |
| 940 | `data_disk_debris_bytes` | 56% | 60 min |
| 1193 | `replication_errors` | 51% | 60 min |
| 1249 | `voter_errors` | 53% | 70 min |
| 1273 / 1301 | `review_bot_merge_change_errors` / `deploy_errors` | 48% | 70 min |
| ~1370 | `review_interrupts_*` | 46% | 75 min |
| ~1400 | `mcp_retire_cap` / `mcp_mem_abort` | 45% / 44% | 75 min |
| 1463 | `g2p_dispatch_errors` | 43% | 85 min |
| 1511 | `mirror_out_of_sync` | 43% | 85 min |

**This measurement discriminates between two competing explanations, and it rejects the one this
ticket was originally written around.** If the gaps were cadence aliasing — a publisher whose
interval drifts against fixed 5-minute buckets — presence would be roughly FLAT across metrics,
because every metric rides the same run. It is not flat. It declines monotonically with position
in the script, from 93% at line 213 to 43% at line 1511, and the two large off-trend dips
(`docker_buildkit_cache_used_percent` and `docker_unaccounted_bytes` at 31%, `var_tmp_used_percent`
at 0%) are exactly the metrics published only on a SUCCESSFUL `du` — the call that stalls.

So there are two publisher mechanisms, both established elsewhere and neither of them jitter:

1. **Truncation.** `rebar-observability.service` is SIGTERM-ed on its 240 s `TimeoutStartSec`
   part-way through the run, so everything after the kill point is never published. Root-caused
   by measurement on bug `ignitable-fuchsia-kawala` (`9313-1fac-9f32-4b07`): two 120 s `du` walks
   of the same ~1.88M-file Docker tree exhaust the budget exactly, and the unit runs at
   `IOSchedulingClass=idle` on an IOPS-saturated host. 55 timeout kills against 197 completed
   runs in 24 h.
2. **Measurement failure.** §2f/§2g/§2h/§2i publish a reading only when the reading was obtained,
   deliberately — there is no honest placeholder for a level.

The nominal cadence contract is `OnUnitActiveSec=5min` measured from the last COMPLETED run plus
`TimeoutStartSec=240`, i.e. a 5-9 minute inter-arrival. The head-of-script metrics hold that
contract (93% present, 10 min max gap). The tail does not, and that is a publisher defect, not
an alarm-tuning parameter.

The Terraform comments this change removes asserted "~22 of 24 periods present is the observed
norm" (92%). That is true only of the first ~213 lines of the script. For every metric this
ticket is actually about, the measured figure is 31-56%.

### What this change is, and is not

**This change alters no threshold, no unit, no metric and no period.** It alters only the rule by
which MISSING data is converted into evidence. That distinction matters for the "retuning an
alarm that has no data is a no-op dressed as a fix" objection: none of these alarms is being
tuned toward green. Two alarms with genuinely breaching values (`gate_scratch_mounted = 0`,
`var_tmp_cleanup_active = 0`) keep firing, and two alarms with NO data at all
(`gate_scratch_disk_high`, `var_tmp_used_percent`) stay RED under the new tuning, because an
entirely empty window still satisfies `M = N` on a `breaching` alarm. What changes is that they
can now CLEAR when data returns, which under `M < N` they could not.

**The publisher fix is upstream and is a hard dependency for the noise to actually stop.** Bug
`9313-1fac-9f32-4b07` is in progress and adds a whole-probe deadline, a reserved tail budget, and
— importantly for this file — dedicated `probe_ok` / `probe_truncated` liveness metrics with
their own alarms in `monitoring_9313.tf`. Once those land they are the canonical dead-publisher
and truncation signals, and the heartbeat role the alarms below carry becomes a second line
rather than the only one. **These windows are sized against the 5-9 minute CONTRACT, not against
today's 85-minute tail gaps**, deliberately: sizing to the defect would bake a 105-minute window
into production monitoring and outlive the bug. Until `9313` lands, the tail alarms will still
produce some false pages — just strictly fewer, and no longer unclearable ones.

### Confirmed live at 20:47Z the same day, and it moved two alarms out of "hardening"

While this change was in review, **four alarms entered ALARM within 8 minutes at 2026-09-05
20:47-20:48Z**, and all four are false:

| alarm | StateReason | actual condition |
|---|---|---|
| `rebar-gerrit-gate-down` | "no datapoints were received for 2 periods and 2 missing datapoints were treated as [Breaching]" | Gerrit HTTPS **200**, connect 0.083 s, total 0.266 s |
| `rebar-mcp-serving-path-down` | "1 datapoint … 2 missing … [Breaching]" | `mcp_healthy` publishing **1** throughout |
| `rebar-root-disk-pressure` | "1 datapoint … 2 missing … [Breaching]" | `root_disk_used_percent` **71-76** against a threshold of 85 |
| `rebar-gerrit-data-disk-high` | "1 datapoint … 2 missing … [Breaching]" | `disk_used_percent` **20**, and for a stretch no datapoints at all |

Every StateReason names missing-data-treated-as-breaching, and every value is healthy. A
`Mirror Guard` CI run failed in the same window with an SSL handshake timeout reaching the host
— an IO fault corroborating a transient reachability dip, not a divergence.

**This changes the status of three alarms in this document.** `rebar-gerrit-gate-down`,
`rebar-mcp-serving-path-down` and `rebar-gerrit-data-disk-high` were widened on a measured
NEAR-MISS (a 10.0-minute worst gap against a 10-minute window) and were honestly labelled
speculative hardening, because at authoring time no false firing had been observed on them. One
has now been observed, for all three, in a single 8-minute window, on exactly the mechanism this
change targets. They are **hardening at authoring time, confirmed by a false firing at 20:47Z on
the same day** — recorded that way rather than by rewriting the earlier reasoning, because the
distinction between "predicted" and "observed" is the thing that makes the prediction worth
anything. `rebar-root-disk-pressure` was already in scope as the Profile A anchor and needed no
change; the event confirms its sizing too.

**It also falsified one of my own premises, and cost two alarms their tighter window.** I had
sized `GerritReachable` (§1) and `mcp_healthy` (§1b) at `N = M = 3` (15 minutes) on the ground
that head-of-script metrics held the cadence contract — the 8-hour sweep said 93% presence and a
10.0-minute worst gap, which supported it. Re-measured immediately after the event, the
head-of-script gap is **25.0 minutes**, which no 15-minute window survives. Truncation now
reaches §1/§1b. Both alarms therefore move to the same `N = M = 6` (30-minute) window as the rest
of Profile A. The tail in the same 3-hour window is worse still: `mirror_out_of_sync` and
`g2p_dispatch_errors` each had **1 datapoint in 3 hours**, newest 130 minutes old.

The event is also a live demonstration of the coupling documented above: truncation produces the
gaps, and `breaching` with `M < N` converts a gap into a page. **Both halves are needed.** This
change removes the second; `ignitable-fuchsia-kawala` removes the first and is still in flight.

## The three invariants that make the failure modes unreachable

**I1 — a `breaching` alarm must have `datapoints_to_alarm == evaluation_periods`.**

This is the whole unclearable story in one line. The 10.5-hour stuck alarm happened because
`M < N` let missing buckets *out-vote* a real datapoint: one healthy reading plus two empty
buckets still satisfied `M = 2`. When `M = N`, a single healthy datapoint anywhere in the window
makes the alarm's condition unsatisfiable, so it returns to OK immediately — no matter how slow
the publisher is. It also makes a lone gap harmless, because a gap can only ever supply *some*
of the required datapoints, never all of them.

**I2 — a `breaching` alarm's window must be at least 900 s.**

`I1` is only safe while a healthy publisher is *guaranteed* at least one datapoint inside the
window; without that, `M = N` is satisfiable by silence alone and the fix does nothing. 900 s is
the next period multiple above the largest gap measured when this was written (10.0 min), itself
above the 540 s contractual bound. **The 20:47Z event later measured a 25-minute head-of-script
gap, which is why every `breaching` alarm here now runs a 1800 s window rather than the 900 s
floor — 900 s is the FLOOR the guard enforces, not the size anything is set to.**

**I3 — a non-`breaching` alarm must stay reachable at the worst-case cadence.**

An alarm that cannot fire is as useless as one that always fires. With missing data no longer
counting toward `datapoints_to_alarm`, every required datapoint must actually arrive inside the
window, so the window must budget at least 10 minutes of publisher time per required datapoint:

    evaluation_periods * period / datapoints_to_alarm >= 600 seconds

10 minutes is the 9-minute contractual bound plus margin, and matches the largest inter-arrival
gap measured at authoring time (10.0 min).

All three are enforced offline by `tests/unit/test_alarm_window_tuning.py`, which uses this same
`I1`/`I2`/`I3` numbering and also seeds the exact pre-fix configurations as failing cases.
Retuning any alarm back to `3 / 2 / breaching` fails the build.

## The two profiles, and how each alarm is assigned

The design fault named on the ticket is conflating **liveness** with **condition** in one metric
and one window. The discriminator that resolves it is a single question per alarm:

> **Does silence on this metric carry information that no other alarm carries?**

### Profile A — silence is evidence: `breaching`, `M = N`

Two kinds of metric answer yes.

*Heartbeats.* `GerritReachable` (§1), `mcp_healthy` (§1b), `gate_scratch_mounted` (§2e),
`journal_cap_in_effect` (§2g), `var_tmp_cleanup_active` (§2h), `container_reaper_active` (§2i)
and `mirror_out_of_sync` (§5). Each publishes a value on every run — `1` healthy / `0` unhealthy,
or `0` in sync — so a healthy period is a published value and never silence. This is the `bff5`
inversion, kept deliberately. Because these seven are spread from §1 to §5, they also catch a
*truncated* run: whichever sections stop being reached, the next sentinel below them goes silent
and pages. That is what preserves dead-publisher detection once the counters stop treating
silence as breach.

*Readings that are only published on a successful measurement.* `disk_used_percent` (both
mounts), `data_disk_debris_bytes`, `root_disk_used_percent`, `docker_storage_used_percent`,
`docker_buildkit_cache_used_percent`, `docker_unaccounted_bytes`, `journal_used_percent`,
`var_tmp_used_percent`, `container_writable_used_percent`. §2/§2c/§2f/§2g/§2h/§2i publish these
only when the measurement worked, deliberately: there is no honest placeholder for a reading, so
`0` would assert an empty volume and `100` would fabricate a full one. Silence therefore means
"the probe could not size this generator" — a real, reachable runtime condition (a `du` that
could not run, a wedged docker daemon, an unmountable data volume), and one
`tests/unit/test_alarm_actions_terraform.py` pins by name under ADR 0112.

It would have been *convenient* to move these gauges to `missing` on the general principle that
a missing reading says nothing about a level. In this tree that principle does not hold, because
here a missing reading is not "we did not look" — it is "we looked and could not see", which is
itself the news. So `breaching` stays and only the evidence rule changes: `M = N` means a gap can
never manufacture a page, and one real reading always decides the alarm.

Windows are sized by publish position, since that bounds the gap:

* `GerritReachable` (§1), `mcp_healthy` (§1b) and everything published from §2 to §2i:
  **N = M = 6** at `period = 300` → 30 minutes. The head-of-script pair was first sized at
  `N = M = 3` (15 min) because the sweep showed it holding the cadence contract; the 20:47Z
  false firing and the 25.0-minute head gap measured with it retired that exemption.
* `mirror_out_of_sync` (§5) is published **last**, making it the tail-of-script sentinel as well
  as the divergence signal: **N = M = 8** → 40 minutes.

### Profile B — silence is not evidence: `notBreaching`, `M < N`

`g2p_dispatch_errors`, `replication_errors`, `review_bot_merge_change_errors`, `voter_errors`,
`deploy_errors`, `review_interrupts_bound_exceeded`, `review_interrupts_signal_unavailable`,
`mcp_retire_cap`, `mcp_mem_abort`.

These are `Sum` over per-interval deltas against `threshold = 0`. Absence of a delta report is
not evidence of errors, and their liveness role is fully covered by the seven Profile A
heartbeats above — the same publisher, the same timer, better signals for the job.

The assignment is *forced*, not chosen. Each of these alarms must keep detecting an
**intermittent** error source: `monitoring_s4b.tf` records the reason explicitly, that the marker
stream is not phase-aligned with CloudWatch period boundaries and can land two markers in one
period and none in the next, so an `M = N` streak would reset on the gap. So `M < N` is a
required property here. `breaching` with `M < N` is precisely the defect. Therefore these alarms
must be `notBreaching`, and the window widens (I3) instead of the datapoint count moving.

## Dead-man coverage for the nine `notBreaching` counters — proven, and its residual

Moving the counters to `notBreaching` makes **"the publisher is dead" and "there were no errors"
the same observation** on those nine metrics. For an error counter that is a check that stops
checking, silently, and only once something is already wrong. So the coverage has to be proven.

**The early heartbeats do NOT cover them.** Measured over the 8-hour sweep: `mcp_healthy` (§1b)
and `gate_scratch_mounted` (§2e) were each present in **54 buckets where `g2p_dispatch_errors`
was absent**. They demonstrably do not stop together — the probe is truncated *between* them,
which is the whole finding above. A heartbeat published before the kill point tells you nothing
about sections after it.

**The tail sentinel does.** `mirror_out_of_sync` is the LAST metric the probe publishes
(observability.sh line 1511, unconditional, on every run including the failed-comparison path),
and all nine counters publish at lines 1193-1463. Publication is sequential, so reaching line
1511 implies every counter's line was reached: **sentinel silence is a superset of counter
silence.** Measured against that prediction, per 5-minute bucket:

| counter | line | buckets present | sentinel present while counter ABSENT |
|---|---|---|---|
| `replication_errors` | 1193 | 49 | 3 |
| `voter_errors` | 1249 | 51 | 3 |
| `review_bot_merge_change_errors` | 1273 | 46 | 3 |
| `deploy_errors` | 1301 | 46 | 3 |
| `review_interrupts_bound_exceeded` | 1391 | 44 | 2 |
| `review_interrupts_signal_unavailable` | 1395 | 44 | 2 |
| `mcp_retire_cap` | 1419 | 43 | 1 |
| `mcp_mem_abort` | 1421 | 42 | 0 |
| `g2p_dispatch_errors` | 1463 | 41 | 0 |

(`mirror_out_of_sync`: 41 buckets.) The residual falls monotonically to zero with proximity to
the sentinel, which is what sequential execution predicts and what a coincidence would not. Set
against the early heartbeats' 54, the discriminator is two orders of magnitude.

The ordering is therefore load-bearing, so it is **enforced, not commented**:
`test_the_tail_sentinel_is_published_after_every_notbreaching_counter` fails the build if
`mirror_out_of_sync` moves earlier or a counter is added after it — either of which would remove
the dead-man from nine alarms with no other symptom.

### The residual, stated rather than left to be discovered

1. **Dead-publisher detection for these nine is SLOWER.** It moves from their own ~15-minute
   windows to the sentinel's 40-minute one. A dead probe pages, but later.
2. **It is a single point of coverage.** All nine now depend on one alarm. The test above is what
   keeps that dependency from being silently broken, but it is a coupling and it is new.
3. **Up to 3 buckets per counter where the sentinel published and the counter did not.** Those
   are conditional-publish paths and bucket-boundary skew, not truncation — a run that completed
   while one counter's own emit was skipped. In exactly those cases the counter's silence is
   genuinely not publisher death, which is what `notBreaching` is for.
4. **Follow-up that retires all three.** Bug `ignitable-fuchsia-kawala` (`9313-1fac-9f32-4b07`),
   in progress now, adds `probe_ok` (published once, last) and `probe_truncated` (from an
   `ExecStopPost` hook that systemd runs even after a timeout SIGTERM), with their own alarms in
   `monitoring_9313.tf`. Those are purpose-built liveness signals and are strictly better than
   borrowing the divergence metric for the job: `probe_truncated` reports the truncation
   directly rather than inferring it from an absence. **When they land, the sentinel duty should
   move to them and this section should be revised** — `mirror_out_of_sync` can then go back to
   being only a divergence signal.

## Per-alarm dispositions

`P` = `period` (s), `N` = `evaluation_periods`, `M` = `datapoints_to_alarm`.

| Alarm | Profile | Before (P/N/M/missing) | After (P/N/M/missing) | Worst-case detection |
|---|---|---|---|---|
| `rebar-gerrit-gate-down` | A heartbeat | 300/2/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-mcp-serving-path-down` | A heartbeat | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-gate-scratch-unmounted` | A heartbeat | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-journal-cap-not-in-effect` | A heartbeat | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-var-tmp-cleanup-not-active` | A heartbeat | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-container-reaper-not-active` | A heartbeat | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-gerrit-mirror-out-of-sync` | A heartbeat | 300/3/2/breaching | 300/8/8/breaching | 40 min |
| `rebar-gerrit-data-disk-high` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-gerrit-data-disk-debris` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-root-disk-pressure` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-gate-scratch-disk-high` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-docker-storage-cap-high` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-docker-buildkit-cache-high` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-docker-unaccounted-bytes` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-journal-usage-high` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-var-tmp-usage-high` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-container-writable-usage-high` | A reading | 300/3/2/breaching | 300/6/6/breaching | 30 min |
| `rebar-gerrit-g2p-dispatch-errors` | B | 300/3/2/breaching | 300/4/2/notBreaching | 20 min |
| `rebar-gerrit-replication-errors` | B | 300/3/2/breaching | 300/4/2/notBreaching | 20 min |
| `rebar-review-bot-merge-change-errors` | B | 300/3/2/breaching | 300/4/2/notBreaching | 20 min |
| `rebar-gerrit-voter-errors` | B | 300/5/3/breaching | 300/6/3/notBreaching | 30 min |
| `rebar-autodeploy-errors` | B | 900/4/2/breaching | 900/4/2/notBreaching | 60 min |
| `rebar-autodeploy-review-interrupts-bound-exceeded` | B | 900/4/2/breaching | 900/4/2/notBreaching | 60 min |
| `rebar-autodeploy-review-interrupts-signal-unavailable` | B | 900/4/2/breaching | 900/4/2/notBreaching | 60 min |
| `rebar-autodeploy-mcp-retire-cap` | B | 900/4/2/breaching | 900/4/2/notBreaching | 60 min |
| `rebar-autodeploy-mcp-mem-abort` | B | 900/4/2/breaching | 900/4/2/notBreaching | 60 min |

Unchanged, and why: `rebar-gerrit-ec2-system-check` and `rebar-gerrit-ec2-instance-check` are
`AWS/EC2` metrics on a 60 s publisher AWS guarantees, so no gap analysis applies;
`rebar-bedrock-invoke-client-errors` is an `AWS/Bedrock` metric already on `notBreaching`;
`rebar-bridge-run-failures` already runs `notBreaching` with a 3600 s period against a different
publisher and satisfies I3 as it stands.

**No threshold and no unit is changed by this ticket.** Every threshold — 85% fullness, the 1 GiB
debris bound, the 2 GiB unaccounted bound, `> 0` on the counters, `< 1` on the health flags — was
measuring the right quantity against the right bound. What was wrong was the evidence rule that
turned "we did not look" into "it breached". Changing a threshold here would have masked that
rather than fixed it.

## Detection preserved, alarm by alarm

The claim to check is that no alarm lost the ability to detect its real condition.

* **Profile A heartbeats.** The condition is a published `0` (or `1` below threshold), which
  recurs on every run while it holds. Every period in the window is then breaching, so `M = N` is
  satisfied and the alarm pages within the window. Confirmed against live state: on 2026-09-05,
  `gate_scratch_mounted = 0` and `var_tmp_cleanup_active = 0` were the two genuinely-true alarms
  of nineteen, and both still fire under this tuning.
* **Profile A readings.** Capacity levels are persistent, not intermittent: a volume at or above
  85% reads so on every run. So all `N` periods breach and the alarm pages. The
  failed-measurement condition (silence) is likewise persistent while it holds, so a whole silent
  window pages too.
* **Profile B counters.** `M` is unchanged for every one of them, and missing periods no longer
  compete for those `M` slots, so an error stream that used to reach `M` real breaching
  datapoints still does. Intermittent, non-phase-aligned streams — the case `monitoring_s4b.tf`
  sized `voter_errors` around — are preserved precisely because `M < N` was kept.

The one thing that is deliberately no longer detected is a gap. That was never a condition.

## Clearing is guaranteed

The unclearable state had one cause: with `breaching` and `M < N`, missing buckets could out-vote
a real datapoint, so a publisher slower than the window re-satisfied `M` on every evaluation
forever. `rebar-docker-buildkit-cache-high` sat in ALARM 10.5 hours that way.

Under this tuning **no alarm can hold ALARM once one healthy datapoint lands in its window**:

* `breaching` with `M = N`: one non-breaching datapoint makes the window fall short of `N`.
  This holds for any window length and any publisher speed, because it does not depend on how
  many datapoints arrive — only that one did.
* `notBreaching` with `M = 2` (or `3`): one healthy datapoint contributes nothing breaching, and
  missing periods contribute nothing either, so the breaching count cannot reach `M` unless `M`
  real breaching datapoints exist.

Neither case can re-arm from silence, because silence no longer supplies the last required
datapoint in either profile.

## What each change costs

Detection latency grows for most alarms, and that is the deliberate trade: the alarms below were
not detecting faster, they were *firing* faster, on nothing.

The one genuine loss is `rebar-gerrit-gate-down`, which goes from a 10-minute to a **30-minute**
detection bound for a genuinely unreachable Gerrit. At `N = M = 2` a single >10-minute
inter-arrival — measured twice in two hours — was enough to page on its own, and on 2026-09-05 at
20:47Z it did exactly that while Gerrit answered in 0.266 s. So the 10-minute detector was not
buying 10-minute detection; it was buying noise. 15 minutes was the first attempt and the same
event's 25.0-minute head-of-script gap ruled it out. Hard host death is still caught in ~2 minutes
by the `AWS/EC2` status-check alarms, which are AWS-published and do not depend on this publisher
at all, so what actually widens to 30 minutes is the narrower "host up, Gerrit not serving" case.
This is the change's biggest single cost and it should be paid back: once
`ignitable-fuchsia-kawala` restores the cadence, `N = M = 3` becomes viable again.

`rebar-gerrit-mirror-out-of-sync` moves from a nominal 15-minute bound to 40 minutes. Replication
lag is normally ~15 s and was measured at up to 2 m 44 s, so a 40-minute continuous divergence is
unambiguous, and the harm it guards — CI testing a different tree than Gerrit's `main` — accrues
over hours. In exchange, the reconstructed firing that opened this ticket — `1, MISSING, 1`, two
independent sub-minute catches of fresh submits — can no longer page, because `M = N` requires
every reading in the window to be `1` and the healthy `0`s between the catches clear it.

## A limitation the 20:47Z event exposed, and does not fix

Profile A's second half rests on "silence means the probe could not take the measurement" —
`disk_used_percent`, `journal_used_percent` and the rest publish only on a successful read, so
their absence is a failed measurement and worth paging on. **Truncation contaminates that
premise.** When the probe is SIGTERM-ed before §2, `disk_used_percent` is absent for a reason
that has nothing to do with the volume, and `rebar-gerrit-data-disk-high` cannot tell the two
apart — as it did not at 20:47Z, when it went ALARM on missing data while the volume sat at 20%.

`M = N` bounds the damage (a page needs the whole 30-minute window silent, and one reading ends
it) but it cannot disambiguate, because the information simply is not in the metric. The fix is
upstream and already designed: `9313`'s `probe_truncated` says "the run was cut short" as a fact
rather than an inference, which is exactly the discriminator these alarms lack. **Until it lands,
a Profile A reading alarm firing on absence should be read as "the probe could not measure this",
not as "this generator is full"** — check `probe_ok` / the unit's journal before treating it as
capacity pressure.

## Revert conditions

Each of these is an observation that would mean this tuning is wrong. They are stated so the next
reader can check the change rather than trust it — the same obligation the false "~22 of 24
periods" comment failed.

1. **The noise does not stop after `9313-1fac-9f32-4b07` lands.** If the probe completes within
   its budget, tail metrics return to >=90% bucket presence, and any Profile A alarm below is
   still entering ALARM while its metric publishes non-breaching values, then `M = N` was not the
   whole defect and the windows are still too narrow. Widen `evaluation_periods`; do not lower
   `datapoints_to_alarm`.
2. **`rebar-docker-unaccounted-bytes` stops firing on a real overshoot.** This is the one alarm
   whose value genuinely crossed its threshold during the measurement window (2.31 GB against a
   2 GiB bound at 10:12 PDT), and it is a RISING metric that crosses intermittently before it
   crosses persistently. `M = N` deliberately waits for the persistent crossing. If, after `9313`
   lands, the metric publishes reliably and sits above 2 GiB for 30 minutes without the alarm
   firing, `M = N` is too strict for a trend metric and this one alarm should move to
   `notBreaching` with `datapoints_to_alarm = 2` of `evaluation_periods = 6`. (Note that the
   metric itself is under repair on bug `regretful-enormous-horsefly` — ~56% of its value is xfs
   block-allocation overhead — so its threshold is not this ticket's to touch.)
3. **A genuinely unreachable Gerrit goes unnoticed for longer than 30 minutes.**
   `rebar-gerrit-gate-down` traded 10-minute detection for 30. If an outage is ever missed past
   that bound while `GerritReachable` was publishing 0, the trade was wrong and the alarm should
   go back to a narrower window with the publisher fixed underneath it. **This one has a positive
   trigger as well as a negative one:** once `ignitable-fuchsia-kawala` lands and head-of-script
   presence returns to >=93% with a worst gap under 10 minutes, `rebar-gerrit-gate-down` and
   `rebar-mcp-serving-path-down` SHOULD be narrowed back to `N = M = 3`. Leaving them at 30
   minutes after the publisher is fixed is itself a defect — it would be tuning permanently to a
   transient fault, which is the thing this document argues against.
4. **A sustained mirror divergence is not paged within 40 minutes.** If `mirror_out_of_sync`
   publishes 1 continuously and `rebar-gerrit-mirror-out-of-sync` does not reach ALARM inside its
   8-period window, `M = N` is defeated by something in the probe (an interleaved 0 that is not a
   genuine in-sync reading), and the divergence signal needs separating from the tail-liveness
   role this tuning gives it.
5. **An alarm holds ALARM through a published healthy datapoint.** That is the exact defect this
   change claims to have made unreachable. One instance falsifies the `M = N` argument outright.

## Alarms deliberately NOT changed by this ticket

* `rebar-gate-scratch-disk-high` and `rebar-var-tmp-usage-high` had **zero datapoints in the
  8-hour sweep**. Their ALARM state is a data-availability defect, not a threshold defect, and no
  alarm-side change can fix it: the upstream causes are the unmounted gate-scratch volume
  (ticket `dcc3`) and the pending `rootflags=pquota` reboot. Their window shape is brought into
  line with the rest so they can clear when data returns, and they correctly stay RED until it
  does. Nothing else about them is this ticket's to touch.
* `rebar-gate-scratch-unmounted` (`gate_scratch_mounted = 0`, 93% present) and
  `rebar-var-tmp-cleanup-not-active` (`var_tmp_cleanup_active = 0`, 69% present) are **correctly
  firing on true conditions**. Their thresholds and comparison operators are untouched, and their
  continued firing under `M = N` is proven by the sweep: every published datapoint in 8 hours is
  breaching, so every period in the window breaches.
