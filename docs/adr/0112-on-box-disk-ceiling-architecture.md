# ADR 0112: The review host's disk is bounded by per-accumulator caps, a dedicated gate-scratch volume, and bounded gate concurrency

- **Status:** Accepted
- **Date:** 2026-09-03
- **Context:** Bug *Gerrit host root volume filled to 100%; five-hour outage*
  (`3276-2f81-8c75-4ddd`), epic *endowed-upset-scaup* (`6202-e1c7-c57f-4897`), authored under its
  story S0 *livid-blondish-shearwater* (`7dfa-18ca-9c04-4691`). This ADR is
  the architecture the epic's stories (S1–S6) implement; it does not itself change infra.
  Extends ADR 0005 (content-addressed snapshot cache + janitor) with the concurrency precondition
  its reclamation model needs; builds on ADR 0012 (the IaC substrate that owns the instance, the
  root volume, and the separate `/var/gerrit` data volume); relates to ADR 0079 and ADR 0104,
  whose `t4g.large` statement is narrowly **memory**-scoped — they say only that the transient 2×
  `rebar-mcp` of a one-in / one-out deploy overlap "never forces the `t4g.large` (8 GiB) up to
  `t4g.xlarge`", and neither says anything about root **volume size** or bars a resize in general,
  so decision 4 declines one on this ADR's own reasoning rather than on their authority;
  interacts with ADR 0069 and ADR 0077 (the `low-disk`
  coverage-gap path that must not become a code veto).

## Context

On 2026-09-02 the `rebar-gerrit` host's **root** volume reached 100% used and stayed there for
roughly five hours. Everything on the box that writes fail-closes at ENOSPC, so the outage took
out Gerrit, the review-bot's `LLM-Review` votes, and the on-box MCP server together.

**This had already happened once, and the alarm we built afterwards did not prevent it.**
Incident 2731 produced `rebar-root-disk-pressure` (`infra/terraform/monitoring_autodeploy.tf`,
bug `ac14`): `rebar/host:root_disk_used_percent`, 85%, a 300 s / 3-period / 2-datapoint window,
`treat_missing_data = "breaching"`. That alarm is correct and it is not the gap. The gap is what
it can *say*: it reports **"root disk high"** and nothing more. It cannot name which of the four
independent accumulators on the volume grew, so the operator response is a `du` under time
pressure — which is exactly what the five hours were spent on.

The measured root working set at the outage was **28G**:

| Accumulator | Size | Notes |
|---|---|---|
| `/var/lib/docker` | 17G | `overlay2` 16G across **67** layer directories |
| `/var/tmp` | 3.6G | gate/investigation scratch |
| `/var/log` | 1.8G | journal 1.7G |

Docker's own accounting (`docker system df`) claimed **~9.5 GB with ZERO dangling images**, so
roughly **6.5 GB of orphaned `overlay2` was invisible to `docker prune` entirely**. That is the
decisive fact about the mitigation actually attempted: four rounds of prune-based reclamation
were measured against a **29 GB** problem and their combined reclaimable ceiling was **~1.06 GB**.
Prune is not a small lever here; it is a lever that structurally cannot reach the bytes that
matter, because the bytes it can reach are not the bytes that grew.

**Root is already 60 GiB** (`var.root_volume_size_gb`, grown from the 30 GiB the alarm's
description still names). Sizing was therefore never the missing control: an *unbounded* generator
fills 60 GiB as readily as 30, and only changes how long the fuse is.

### The finding that makes bounded concurrency a precondition, not a nice-to-have

`src/rebar/_snapshot/janitor.py` documents its own reclamation contract, and it is deliberate:
it relies on POSIX **delete-on-last-close** plus touch-on-read `mtime` recency, evicts by
`rename` into `trash/` then `rmtree` (so open fds survive), and **never takes a per-reader
lease** — a PID+heartbeat lease was spiked and **rejected as unsound** (N readers per entry, PID
reuse, crash-stale leases), following Gitaly, Sourcegraph gitserver, Bazel and ccache. It also
skips any entry touched inside a short grace window.

The consequence is the thing this ADR exists to explain. The janitor can **unlink** an in-flight
snapshot, but the **blocks are not returned until the last reader closes**. Under the operator's
stated normal load of ~10 concurrent reviews, the bytes those readers hold are **LIVE, not
garbage** — and a high-water-mark GC has nothing to evict. A cap set below peak concurrent hold
therefore **cannot be honoured by reclamation**; the janitor will run, evict what it may, and the
volume will still be full.

Note the symmetry with the failure being fixed. Prune could not reach 6.5 GB of orphaned
`overlay2`; a watermark GC cannot reach bytes held open by live readers. Both are reclaimers
pointed at bytes that are structurally out of their reach, and in both cases adding more
reclamation is the wrong response. The only control that bounds live held bytes is a bound on
**how many holders there can be**.

The hold also does not shrink with cache sharing. The snapshot store is content-addressed
(ADR 0005), so N concurrent reviews of the *same* base share one entry — but the review-bot's
working clone is **per-review** (`src/rebar/review_bot/voter.py:381`,
`tempfile.TemporaryDirectory(prefix="reviewbot-")`), so clone bytes scale **linearly** with
concurrency regardless.

### The memory budget forbids solving this by growing the host

The box is a `t4g.large` (8 GiB), shared: gerrit 0.88 GB RSS, mcp 1.08, review-bot 0.17, opcert
0.04, with gerrit reserving **~3 GiB by config** (`gerrit.config` `heapLimit 2g` +
`packedGitLimit 1g`). A complete plan-review costs **~739 MB resident delta over ~4.7 min**
(measured 2026-09-03 via the `GATE_PEAK_RSS` marker); a partial/degraded run cost **501 MB**, so
per-run cost varies materially. Concurrency is thus already RAM-bounded well below "as many as
arrive", and the same counter that bounds disk hold bounds memory.

## Decision

### 1. Bound each accumulator at its own source, using that consumer's native GC (Option A)

Every accumulator on the root volume gets a **hard cap enforced by its own retention mechanism**,
not by an external sweeper:

- **Docker `overlay2` + BuildKit build cache** — BuildKit's own cache-GC policy and image
  retention (S2).
- **`/var/tmp`** — an age/size retention policy (S4).
- **journald** — `SystemMaxUse` (S3).
- **Writable container layers** — bounded per container (S5).

A cap at the source is the only kind that holds, because it refuses the *write* rather than
chasing the bytes afterwards. Bug `3276` measured what the alternative buys: ~1.06 GB of
reclaimable ceiling against a 29 GB problem.

### 2. Every capped generator carries its OWN alarm, at or near its cap

A cap with no alarm converts a loud outage into a silent refusal, and an aggregate percentage
alarm cannot name the saturated generator. So **each cap in decision 1 ships with its own
CloudWatch alarm**, and `rebar-root-disk-pressure` is demoted to the backstop it should always
have been.

These follow the established house idiom rather than inventing one: a custom
`rebar/host:<metric>` published by `scripts/observability.sh` on the 5-minute cadence, `Maximum`
statistic, `period = 300` with `evaluation_periods = 3` / `datapoints_to_alarm = 2` (absorbing
ordinary timer jitter), `treat_missing_data = "breaching"`, and both `alarm_actions` and
`ok_actions` on `aws_sns_topic.alerts`. Per-mount metrics carry the `InstanceId` + `mount`
dimension pair; dimensionless host gauges stay dimensionless on **both** sides.

The direct precedent is `rebar-gerrit-data-disk-debris` (task `3e92`): its whole argument is that
`disk_used_percent` answers "how full" but not "full **of what**", and that an alarm on the
*generator* fires before capacity pressure exists — "at 85% used the volume is already an
incident". This ADR generalises that argument from the data volume to the root volume.

### 3. Review-gate scratch moves to a dedicated EBS volume (Option C)

The gate's snapshot store and the review-bot's per-review clones move off the root filesystem
onto a **dedicated EBS volume**, so review work can never fill the OS disk. This mirrors the
existing split that ADR 0012 already established for Gerrit's own data (`rebar-gerrit-data`, a
separate gp3 volume mounted at `/var/gerrit`, not the root) — the box already has one volume
whose exhaustion is survivable-in-isolation, and gate scratch is the second obvious candidate.

The seam is already parameterised: the snapshot store's base directory is `REBAR_GATE_TMPDIR`
(env-only, defaulting to the system temp dir and explicitly **never** a hardcoded `/tmp`), so
relocating the store is configuration, not code.

### 4. Root stays 60 GiB — that is headroom, not the fix

No root resize. The volume was already doubled once and filled again; growing it a second time
buys latency before the same failure and nothing else. This is recorded as a decision so it is
not silently revisited as a first response next time.

### 5. Bounded concurrency with FAST-FAIL admission (S6)

A configurable cap bounds how many **plan-review or completion-verifier** executions may run at
once. Both snapshot the repo at a ref and both hold snapshot bytes and gate RSS for minutes, so
they share **one counter**, not one each — two separate caps of N each admit 2N holders, which is
precisely the bound that was needed.

**At capacity the call fast-fails with an explicit congestion message ("try again later"); it does
not queue.** Queueing is rejected for three reasons: a queued gate still holds the client's
request open past the MCP client's ~60 s deadline (producing the `-32001` ambiguity the async
`*_start` + poll surface exists to avoid); a queue is itself unbounded storage of the thing being
bounded; and a refusal is *legible* — it lets clients modulate load, which a silent wait does not.

This extends an admission seam that already exists rather than adding a parallel one:
`src/rebar/review_bot/low_disk.py` + `src/rebar/_snapshot/janitor.py`'s
`ensure_min_free_space` / `SnapshotLowDiskError` already refuse **before** cloning, on a free-space
floor (`DEFAULT_MIN_FREE_GIB`). Concurrency is the second admission term on the same gate.

**On the review-bot path the congestion refusal follows ADR 0069's retryable-deferral shape, and
must never become a `LLM-Review −1`.** ADR 0069's load-bearing carve-out is that `low-disk`
"is an operator/host condition and must not become a false code veto"; host congestion is the
same class of condition, and the same reasoning binds. Whether it reuses the `low-disk`
coverage-gap sub-reason or takes its own is an implementation choice for S6, but the fail-closed
`−1` is not available to it.

### 6. Cap values are measured defaults, settable in config — never hardcoded constants

Every cap in decisions 1 and 5 derives its **default** from measurement (the 28G working-set
breakdown; the ~739 MB / ~4.7 min gate cost; the ~10 concurrent-review normal load) and is
**operator-settable**. The gate-side knobs follow the existing `[snapshot]` resolution order —
`REBAR_GATE_*` env > the `[snapshot]` config table > built-in default — which is where
`free_watermark_bytes`, `max_bytes`, `max_entries` and `min_free_gib` already live; the
host-side caps are terraform variables and unit/config files, like `root_volume_size_gb`.

A default is a starting point sized from one host's measurement, and the measurement is known to
vary run-to-run (739 MB vs 501 MB on the same operation). Freezing any of these as a constant
would make the one knob an operator needs during the next incident unreachable without a deploy.

### 7. Host sizing is NOT reopened

ADR 0079's 2026-08-24 amendment and ADR 0104 §2 both state that MCP deploy overlap is capped
one-in / one-out "guarded by a memory-pressure alarm, so the transient 2× `rebar-mcp` never forces
the `t4g.large` (8 GiB) up to `t4g.xlarge`". This ADR does not reverse that: the host stays
`t4g.large`, and decision 5 is the mechanism that keeps concurrent gate RSS inside the 8 GiB
budget those ADRs assume rather than the mechanism that escapes it.

Recorded precisely, because the constraint's scope matters: those statements are **memory**-scoped
and specific to deploy overlap, and neither says anything about **root volume size**. Decision 4
declines a resize on its own merits (an unbounded generator fills any volume), not by borrowing
authority from ADR 0079/0104.

## Consequences

- **Bug `3276`'s missing-data alarm fix must not regress, and no child story currently asserts
  it.** The plan-review gate flagged this against epic AC4. `treat_missing_data = "breaching"` on
  the root/data disk alarms is the property that stops a dying host clearing its own disk-pressure
  alarm to OK; every alarm added under decision 2 must carry it, and the implementation must add a
  test that pins it rather than relying on copy-paste. **This is an implementation obligation
  carried by this ADR, not by any single story.**
- **S2 and S5 are one budget, not two.** Writable container layers live **inside** `overlay2`, so
  an independent cap on each is either double-counted or mutually violable. They must be specified
  as a single `/var/lib/docker` budget with an internal split, and their alarms must not be able to
  disagree about how full the same bytes are.
- **S4's `/var/tmp` cap conflicts with the documented evidence convention and must not evict
  evidence mid-investigation.** `infra/runbooks/review-bot-ops.md` and
  `infra/runbooks/gerrit-data-volume-reclaim.md` both direct operators to stage investigation
  output under `/var/tmp/rebar-evidence/<ticket>-<stamp>/` with a `trap`-based delete. A blind
  age/size sweep of `/var/tmp` would delete an active incident's evidence — during exactly the
  incident it was collected for. S4 must either exempt that prefix or cap it separately with its
  own alarm; it may not simply reuse a generic `/var/tmp` policy.
- **The root-disk alarm's description is stale and should be corrected in passing.** It still says
  "The box's 30G ROOT disk"; `var.root_volume_size_gb` is 60. An alarm whose text misstates the
  volume it watches is read during an incident.
- **Fast-fail is client-visible behaviour, so it must be documented as an expected outcome.**
  Agents calling `review_plan` / `verify_completion` will now see a congestion refusal that is
  neither a gate failure nor an error. The async `*_start` + poll surface and
  `docs/plan-review-gate.md` need to name it, or the first occurrence will be debugged as an
  outage.
- **These stories will trip the mechanism-delta ratchet, by design.** Decisions 1, 2 and 5 add
  `config_key` / `ci_gate` mechanisms; `scripts/check_mechanism_delta.py --check` fails on any
  `new > 0`. Each needs an in-tree `# mechanism-ok: <kind> <name> — <reason or ticket id>` marker
  at its definition site. The ratchet is not an obstacle here — it is the thing that forces the
  justification for each new knob to be written down next to the knob.
- **A dedicated scratch volume adds a mount that can fail independently.** It needs its own
  `disk_used_percent` alarm on the `InstanceId` + `mount` dimensions, and gate admission must
  treat "scratch volume unmounted" as a refusal, not as an empty cache to repopulate onto the root
  filesystem — otherwise the volume's failure mode is silently reverting to the state this ADR
  exists to prevent.

## Open items

- The concurrency cap's **default value** is not fixed by this ADR. It must be derived from a
  measured peak concurrent hold (snapshot bytes + per-review clone bytes + gate RSS) against the
  8 GiB / scratch-volume budgets, in S6.
- Whether the congestion refusal reuses ADR 0077's `low-disk` first-line tag vocabulary or adds a
  sibling sub-reason is left to S6; either way it routes through ADR 0069's retryable path.
- Enforcement of op-cert environment binding remains **advisory** (ADR 0104 decision 3 —
  `verify.require_environment` + `verify.opcert_enforce_since` are both unset in the committed
  `rebar.toml`). Nothing in this ADR changes that, and the fast-fail path must not be the thing
  that flips it.

## Prior art / grounding

- **ADR 0005** — content-addressed snapshot cache + janitor. This ADR supplies the concurrency
  precondition its watermark reclamation needs; the lease-free, delete-on-last-close design is
  correct and is **not** being reopened.
- **ADR 0012** — the IaC substrate: the `t4g.large` instance, the root volume, and the separate
  `rebar-gerrit-data` gp3 volume whose split decision 3 mirrors. ADR 0012 decides no volume sizes
  and no alarms, so this ADR is not overriding it.
- **ADR 0069 / ADR 0077** — the `low-disk` retryable coverage gap and its fail-closed vote mapping.
  A host condition defers; it does not veto code. Decision 5 inherits that rule.
- **ADR 0079 (2026-08-24 amendment) / ADR 0104 §2** — a **memory**-scoped statement only: MCP
  deploy overlap's transient 2× `rebar-mcp` may not force the `t4g.large` up to `t4g.xlarge`.
  Neither ADR decides root volume size or prohibits a resize. Decision 7 records that exact scope.
- **`rebar-gerrit-data-disk-debris`** (`infra/terraform/monitoring.tf`, task `3e92`) — the
  in-repo precedent for alarming on the *generator* rather than on aggregate fullness.
- **`rebar-root-disk-pressure`** (`infra/terraform/monitoring_autodeploy.tf`, bug `ac14`,
  incident 2731) — the existing backstop alarm, and the demonstration that an aggregate
  percentage alarm is necessary but not sufficient.
- **Gitaly, Sourcegraph gitserver, Bazel, ccache** — the mature-system precedent, cited in
  `janitor.py`, for leaning on kernel lifetime guarantees instead of per-reader leases.
