# ---------------------------------------------------------------------------
# monitoring_autodeploy.tf — CloudWatch alarm for continuous-auto-deploy failures.
# Epic 88ab / story 8903.
# ---------------------------------------------------------------------------
# METRIC SOURCE: the on-box systemd oneshot rebar-autodeploy.service (autodeploy.sh) writes
# a structured `AUTODEPLOY_ERROR` marker to stderr -> journald on any deploy-step failure
# (git fetch, config-check, materialise, review-bot build/health-check-then-rollback). The
# HOST observability probe (infra/scripts/observability.sh §4d) greps the unit journal for
# those markers and publishes a per-period count to rebar/host:deploy_errors — the same
# host-grep pattern §4/§4c use for voter_errors / merge-change errors. This alarm watches it.
#
# WHY IT MATTERS: a persistent signal means the box is NOT tracking `main` (config/code
# drift) and/or a deploy keeps failing and backing off. The auto-deploy is fail-safe (the
# last-known-good review-bot + config stay live, so the gate is NOT frozen), but sustained
# failure means fixes to `main` are not reaching production — investigate the deploy loop
# (journalctl -u rebar-autodeploy) and the target `main` tip.
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = deploy_errors
#   Dimensions = NONE — dimensionless on BOTH sides (see monitoring_s4b.tf rationale).
#   Unit       = Count   (per-period count of new AUTODEPLOY_ERROR journal lines)
#
# ACTION: wires the shared SNS alerts topic (not a silent alarm), like WS7 / 1fa8 / §4c.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "deploy_errors" {
  alarm_name        = "rebar-autodeploy-errors"
  alarm_description = <<-EOT
    rebar continuous auto-deploy failures detected in the rebar-autodeploy.service journal
    (AUTODEPLOY_ERROR markers: fetch / config-check / materialise / review-bot build or
    health-check-then-rollback). Published as rebar/host:deploy_errors by the host
    observability probe (§4d). The deploy is fail-safe (last-known-good stays live, gate not
    frozen), but a sustained signal means `main` is not reaching the box — investigate
    `journalctl -u rebar-autodeploy` and the target main tip. Disable via
    `systemctl disable --now rebar-autodeploy.timer`; the manual deploy path still works.
  EOT

  namespace   = "rebar/host"
  metric_name = "deploy_errors"
  statistic   = "Sum"

  # Cadence MUST match the deploy's capped backoff (autodeploy.sh BACKOFF_CAP=900s):
  # once backed off, failures arrive at most once per 15 min, so two CONSECUTIVE
  # 5-minute periods > 0 essentially never happen — the original 300s/2-consecutive
  # shape stayed silent through 41h of continuous deploy failure (incident 2731,
  # bug ac14). 15-minute periods with 2-of-4 datapoints latch a persistent failure
  # loop within ~an hour while a single transient error still doesn't page.
  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # DEAD-PUBLISHER, not "quiet when healthy" (ticket bff5-9163-cddd-4158). The host probe
  # publishes deploy_errors' per-interval delta UNCONDITIONALLY every 5 minutes, so a healthy
  # period publishes 0 — the metric is continuously present. Missing data therefore means the
  # PROBE, its timer, or the host is dead, which is exactly when this alarm must page.
  # The 2-of-4 window above already absorbs the jitter this setting introduces.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Epic    = "88ab"
    Story   = "8903"
  }
}

# ---------------------------------------------------------------------------
# Reviews KILLED by a deploy (bug 34cd).
#
# `docker compose up -d` stops the review-bot container, and uvicorn's shutdown drain covers
# only the webhook QUEUE — the backfill reconciler's inline review is cancelled outright, and
# that is the path that RETRIES a killed review. On 2026-08-03 seven recreations in 90 minutes
# (gaps of 18/7/22/4/15/20 min) repeatedly killed a ~10-minute review and changes 1302/1303 sat
# `Verified +1` with `LLM-Review = 0` for 20-35 minutes, unsubmittable.
#
# WHY THIS ALARM EXISTS AT ALL: that outage was INVISIBLE to every other alarm. A killed review
# fails nothing — the process was asked to stop — so the fail-closed voter path never runs, no
# VOTER_ERROR is emitted, `restarts` stays 0, and the deploy itself logs "redeployed + healthy".
# All 11 alarms read OK while the gate was live-locked. `rebar-gerrit-voter-errors`
# (monitoring_s4b.tf) provably CANNOT observe this failure mode, which matters because the
# Bedrock cutover (eb6e) names that alarm as its safety net. autodeploy.sh now defers a deploy
# that would interrupt a review; the alarms below watch the cases where it recreated anyway — the
# DEPLOY_DEFER_MAX bound was exhausted (a chronically busy bot), or the /health in_flight signal
# was unreadable so the deploy ran blind. Routine, healthy deferrals are a SEPARATE metric
# (deploy_deferrals) and deliberately do not page.
#
# SPLIT BY REASON (bug 613a). One alarm on a rolled-up counter could not say WHICH of those two
# causes fired, and they want opposite responses. A CloudWatch-only sweep therefore had to hedge:
# [rebar:7b4a-0f39-1a45-4ce9] was filed unable to distinguish "the bot is busy" from "34cd has
# regressed and every deploy is blind", and closing it needed SSM shell access to read the
# journal. There is now ONE ALARM PER REASON, each carrying only its own remediation, so the
# alarm itself answers the question. Split via distinct METRIC NAMES rather than a CloudWatch
# dimension — every metric here stays dimensionless on both sides (monitoring_s4b.tf: a dimension
# present on only one side silently unmatches and the alarm goes permanently INSUFFICIENT_DATA).
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = review_interrupts_bound_exceeded     — alarmed below
#                review_interrupts_signal_unavailable — alarmed below
#                review_interrupts                    — rolled-up total, published but NOT
#                                                       alarmed: it keeps pre-split history
#                                                       readable and catches any future reason,
#                                                       while leaving each firing to page once.
#   Dimensions = NONE — dimensionless on BOTH sides (see monitoring_s4b.tf rationale).
#   Unit       = Count  (per-period count of new AUTODEPLOY_REVIEW_INTERRUPT journal lines
#                        matching that reason)
#
# Both alarms kept the pre-split 900s / 1-datapoint / Sum > 0 cadence, so the split itself did
# not change aggregate sensitivity. Ticket bff5-9163-cddd-4158 then moved both to 2-of-4 with
# treat_missing_data = "breaching": these counters publish 0 on the healthy path, so silence is a
# dead probe rather than calm, and a 1-datapoint latch on "breaching" would page on one absent
# period. Making `bound-exceeded` less trigger-happy on the SIGNAL side is still deliberately NOT
# bundled in here — that is a call to make with the DEPLOY_DEFER_MAX disposition.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "review_interrupts_bound_exceeded" {
  alarm_name        = "rebar-autodeploy-review-interrupts-bound-exceeded"
  alarm_description = <<-EOT
    An auto-deploy recreated the review-bot mid-review, killing an LLM-Review: the
    DEPLOY_DEFER_MAX deferral bound was exhausted with reviews STILL in flight and the deploy
    proceeded anyway (AUTODEPLOY_REVIEW_INTERRUPT, reason `bound-exceeded`, in the
    rebar-autodeploy.service journal; published as rebar/host:review_interrupts_bound_exceeded
    by the host probe, observability.sh 4e). MEANING: the drain check is WORKING — the bot is
    chronically busy and a deploy ran out of patience. The backfill reconciler retries killed
    reviews, so a lone firing in a landing burst is not an outage. REMEDIATION: repeated
    firings are a review-THROUGHPUT problem, not a probe problem — check review duration and
    concurrency, and whether DEPLOY_DEFER_MAX is sized for them; left alone they live-lock the
    gate (changes reach `Verified +1` with `LLM-Review = 0` and cannot submit). If
    rebar-autodeploy-review-interrupts-signal-unavailable is also firing, that one is primary.
  EOT

  namespace   = "rebar/host"
  metric_name = "review_interrupts_bound_exceeded"
  statistic   = "Sum"

  # Cadence matches the deferral bound (autodeploy.sh DEPLOY_DEFER_MAX=2400s): a bound-exceeded
  # interrupt cannot recur faster than one per episode. The pre-bff5 shape latched on a SINGLE
  # 900s period; with treat_missing_data = "breaching" below, a 1-datapoint latch would also page
  # on one absent period, so the window is 2 breaching datapoints out of 4. A real interrupt loop
  # recurs on the next deploy and a genuinely dead probe stays absent, so either reaches the
  # second datapoint — the cost is detection at ~30 min instead of ~15.
  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # DEAD-PUBLISHER, not "quiet when healthy" (ticket bff5-9163-cddd-4158). The host probe
  # publishes review_interrupts_bound_exceeded's per-interval delta UNCONDITIONALLY every 5 minutes, so a healthy
  # period publishes 0 — the metric is continuously present. Missing data therefore means the
  # PROBE, its timer, or the host is dead, which is exactly when this alarm must page.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "34cd"
  }
}

resource "aws_cloudwatch_metric_alarm" "review_interrupts_signal_unavailable" {
  alarm_name        = "rebar-autodeploy-review-interrupts-signal-unavailable"
  alarm_description = <<-EOT
    An auto-deploy could not read the review-bot's /health `in_flight` signal and recreated the
    container with NO drain check at all (AUTODEPLOY_REVIEW_INTERRUPT, reason
    `signal-unavailable`, in the rebar-autodeploy.service journal; published as
    rebar/host:review_interrupts_signal_unavailable by the host probe, observability.sh 4e).
    MEANING: this is the URGENT one. The drain check itself is broken, so EVERY deploy is now
    running blind and killing whatever review is in flight — the exact failure mode bug 34cd
    exists to catch, silently regressed. It leaves voter_errors, restarts and the deploy log all
    green, so no other alarm can see it. REMEDIATION: confirm the review-bot is up and its
    /health is reachable FROM THE HOST and still returns an `in_flight` field — autodeploy.sh
    bot_in_flight_reviews echoes -1 for unreachable/missing/unparseable and that fail-open path
    is what fires this. It recurs on every deploy while broken, hence the 1-datapoint latch.
  EOT

  namespace   = "rebar/host"
  metric_name = "review_interrupts_signal_unavailable"
  statistic   = "Sum"

  # This case must surface fast, hence the tight 2-of-4 window rather than a slower one. It
  # cannot be the pre-bff5 1-of-1 latch any more: with treat_missing_data = "breaching" below a
  # single absent period would page, and absent periods are ordinary timer jitter. The failure
  # this alarm watches recurs on EVERY deploy while broken, so it reaches 2 datapoints quickly.
  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # DEAD-PUBLISHER, not "quiet when healthy" (ticket bff5-9163-cddd-4158). The host probe
  # publishes review_interrupts_signal_unavailable's per-interval delta UNCONDITIONALLY every 5 minutes, so a healthy
  # period publishes 0 — the metric is continuously present. Missing data therefore means the
  # PROBE, its timer, or the host is dead, which is exactly when this alarm must page.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "34cd"
  }
}

# ---------------------------------------------------------------------------
# Root-filesystem disk pressure (incident 2731). The box's 60G ROOT disk holds
# docker's image/build-cache storage and the review-bot's working tmp; when it
# filled, every LLM-Review fail-closed (clone/pip ENOSPC) — yet the only disk
# metric published was the /var/gerrit EBS data volume, and NO alarm watched
# even that. The host probe (observability.sh §2) now also publishes the root
# filesystem as rebar/host:root_disk_used_percent — DIMENSIONLESS on both sides
# (the monitoring.tf / GerritReachable convention: CloudWatch keys a metric by
# namespace+name+dimensions, so a dimension on only one side silently unmatches).
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = root_disk_used_percent
#   Dimensions = NONE
#   Unit       = Percent  (df used% of /)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "root_disk_pressure" {
  alarm_name        = "rebar-root-disk-pressure"
  alarm_description = <<-EOT
    The rebar box's 60G ROOT filesystem is above 85% used. This is the BACKSTOP
    (ADR 0112 decision 2): it says "root disk high" and cannot name which of the four
    accumulators grew, so read the per-generator alarms FIRST —
    rebar-docker-storage-cap-high, rebar-docker-buildkit-cache-high and
    rebar-docker-unaccounted-bytes — before reaching for `du`. Gate scratch has its own
    volume and its own alarms. Published as rebar/host:root_disk_used_percent by
    observability.sh §2b (5-min cadence).
  EOT

  namespace   = "rebar/host"
  metric_name = "root_disk_used_percent"
  statistic   = "Maximum"

  # Probe cadence is 5 min; 2-of-3 periods over 85% pages within ~15 min of
  # sustained pressure without paging on one anomalous sample.
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # Missing data means the host-published disk probe stopped; page rather than
  # letting a dying host clear its own disk-pressure alarm to OK.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "ac14"
  }
}

# ---------------------------------------------------------------------------
# Review-gate scratch volume (ADR 0112 decisions 2+3, story aa40-cbda-ee38-481c)
# ---------------------------------------------------------------------------
# The scratch volume takes gate snapshots and reviewbot-* clones OFF the root
# filesystem, which is what stops a review burst wedging the OS disk. But it also
# adds a mount that can fail independently of root — ADR 0112 says so directly —
# so it gets its own pair of alarms rather than riding the root alarm above.
#
# TWO alarms, because they answer two different questions and the 3e92 precedent is
# explicit that the second one is not implied by the first: "how full is it" cannot
# say "is it even there". A volume that silently failed to mount reads as 0% used —
# perfectly healthy — while every gate on the box refuses.

resource "aws_cloudwatch_metric_alarm" "gate_scratch_disk_high" {
  alarm_name        = "rebar-gate-scratch-disk-high"
  alarm_description = <<-EOT
    The dedicated review-gate scratch volume is above 85% used. Gate snapshots
    (rebar-gate-snapshots) and the review-bot's per-review reviewbot-* clones live
    here; exhaustion refuses gates instead of taking the OS disk with it (ADR 0112
    decision 3). Reclaim runs through the snapshot janitor; the contents are
    REBUILDABLE, so a stuck volume may be cleared wholesale. Published as
    rebar/host:disk_used_percent with mount=/var/lib/rebar/gate-scratch by
    observability.sh 2e (5-min cadence).
  EOT

  namespace   = "rebar/host"
  metric_name = "disk_used_percent"
  statistic   = "Maximum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
    mount      = var.gate_scratch_mount
  }

  # The house 300/3/2 shape (root_disk_pressure above, gerrit_data_disk_high in
  # monitoring.tf): 2 breaching datapoints in a 3-period window absorbs the ordinary
  # timer jitter that makes ~2 of 24 periods absent on this box.
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # Bug 3276's defect 2 in one line: an alarm whose metric stops must PAGE, not clear
  # itself to OK. Pinned by tests/unit/test_alarm_actions_terraform.py so this is not
  # a copy-paste that a later edit can quietly drop.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "aa40"
  }
}

resource "aws_cloudwatch_metric_alarm" "gate_scratch_unmounted" {
  alarm_name        = "rebar-gate-scratch-unmounted"
  alarm_description = <<-EOT
    The review-gate scratch volume is NOT mounted at its expected path. rebar's gate
    admission refuses every plan-review and completion-verifier run in this state
    (GateScratchUnavailableError) rather than repopulating the snapshot store on the
    ROOT filesystem, so the visible symptom is "all gates refuse", not disk pressure.
    Remount the volume (see infra/runbooks/review-bot-ops.md). Published as
    rebar/host:gate_scratch_mounted, a 1/0 heartbeat on every probe tick.
  EOT

  namespace   = "rebar/host"
  metric_name = "gate_scratch_mounted"
  statistic   = "Minimum"

  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics: the healthy path publishes 1 on EVERY tick, so absence means
  # the probe, the timer, or the host is dead — the one state this alarm most needs to
  # announce.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "aa40"
  }
}

# ---------------------------------------------------------------------------
# Docker storage generators (ADR 0112 decisions 1+2, story 9183-aaae-667d-45e6)
# ---------------------------------------------------------------------------
# rebar-root-disk-pressure above says "root disk high" and nothing more. On 2026-09-02 that
# cost five hours: /var/lib/docker was 17G of a 28G working set, overlay2 alone 16G across 67
# layer directories, and naming the generator took a `du` under time pressure.
#
# THREE alarms, because each answers a question the other two structurally cannot, which is the
# rebar-gerrit-data-disk-debris (task 3e92) argument generalised from the data volume to root:
#
#   1. storage-cap-high      — is the whole /var/lib/docker budget saturating?
#   2. buildkit-cache-high   — is the BUILDKIT GENERATOR at ITS OWN cap? BuildKit sitting at
#                              100% of its 5 GiB share is invisible inside a 20 GiB budget
#                              reading 60%; conversely a runaway image set saturates the
#                              budget while BuildKit sits at 10%. Neither implies the other.
#   3. unaccounted-bytes     — how much of the Docker root does DOCKER ITSELF NOT KNOW ABOUT?
#                              This is the one the incident turned on. `docker system df`
#                              reported ~9.5 GB with ZERO dangling images against 16G of real
#                              overlay2, so ~6.5 GB was unreachable by any prune — four rounds
#                              recovered ~1.06 GB against a 29 GB problem. Alarms 1 and 2 say
#                              "how full"; only this one says "full of what prune cannot
#                              touch", and it is what changes the remediation from "prune
#                              harder" (which is measured to fail) to a daemon-level reclaim.
#                              It is deliberately NOT scoped to overlay2: which subdirectory
#                              holds layer bytes is an engine detail that moves (graphdriver
#                              vs containerd snapshotter), and an alarm scoped to the wrong
#                              one reads healthy forever.
#
# Published dimensionless by observability.sh 2f on the 5-minute cadence, following
# root_disk_used_percent: CloudWatch keys a metric by namespace+name+dimensions, so a
# dimension on only one side silently never matches. The caps behind the two percent metrics
# come from infra/scripts/docker-storage-cap.sh, the same file that renders the daemon's own
# builder.gc policy, so "percent of cap" can never mean a different cap than the one enforced.

resource "aws_cloudwatch_metric_alarm" "docker_storage_cap_high" {
  alarm_name        = "rebar-docker-storage-cap-high"
  alarm_description = <<-EOT
    /var/lib/docker is above 85% of its configured budget (ADR 0112 decision 1; the budget and
    its BuildKit/image split live in infra/scripts/docker-storage-cap.sh). This is the whole
    Docker accumulator, measured from the FILESYSTEM (`du`), not from `docker system df` —
    so it rises with bytes Docker's own accounting cannot see. Check
    rebar-docker-buildkit-cache-high and rebar-docker-unaccounted-bytes FIRST: they name
    which generator grew, and the second one decides whether pruning can help at all. Never
    delete under /var/lib/docker by hand. Published as rebar/host:docker_storage_used_percent
    by observability.sh 2f (5-min cadence). Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "docker_storage_used_percent"
  statistic   = "Maximum"

  # The house 300/3/2 shape (root_disk_pressure above): 2 breaching datapoints in a 3-period
  # window absorbs the ordinary timer jitter that makes ~2 of 24 periods absent on this box.
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # observability.sh 2f publishes ONLY on a successful measurement, so absence means the probe
  # could not read the disk — which is exactly when this must page rather than clear to OK
  # (bug 3276 defect 2). Pinned by tests/unit/test_alarm_actions_terraform.py.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "9183"
  }
}

resource "aws_cloudwatch_metric_alarm" "docker_buildkit_cache_high" {
  alarm_name        = "rebar-docker-buildkit-cache-high"
  alarm_description = <<-EOT
    The BuildKit build cache is above 85% of ITS OWN share of the Docker budget. This is a
    GENERATOR alarm, not a capacity alarm: it fires while the volume is still comfortable,
    which is the point — at 85% of the root disk the box is already an incident. The cache is
    capped by the daemon's own builder.gc policy (/etc/docker/daemon.json, installed by
    infra/scripts/docker-storage-cap.sh), so a cache ABOVE its cap means the policy did not
    take effect: check that daemon.json carries the key this engine honours (maxUsedSpace at
    Engine >= 25.0, defaultKeepStorage below) and that the daemon reloaded. Published as
    rebar/host:docker_buildkit_cache_used_percent by observability.sh 2f.
  EOT

  namespace   = "rebar/host"
  metric_name = "docker_buildkit_cache_used_percent"
  statistic   = "Maximum"

  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # Silence here means `docker system df` did not answer — a wedged or dead daemon, which is
  # never the healthy reading it would otherwise be mistaken for.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "9183"
  }
}

resource "aws_cloudwatch_metric_alarm" "docker_unaccounted_bytes" {
  alarm_name        = "rebar-docker-unaccounted-bytes"
  alarm_description = <<-EOT
    More than 2 GiB exists under /var/lib/docker that `docker system df` does NOT account for
    in ANY of its rows. MEANING: these bytes are unreachable by `docker prune` — the daemon
    does not know they are there. At the 2026-09-02 outage this was ~6.5 GB of orphaned
    overlay2 and four prune rounds recovered ~1.06 GB against a 29 GB problem, so "prune
    harder" is the WRONG response here. DIAGNOSIS: the rebar-health log line published beside
    this metric carries the root, overlay2 and ledger readings, which is what names the
    subtree. REMEDIATION: stop the build path, then a daemon-level reclaim (`docker system
    prune -a` with the serving containers up, or a daemon restart scheduled per the runbook).
    NEVER rm anything under /var/lib/docker: the layer metadata is the daemon's, and deleting
    behind its back desynchronises it from the tree. Published as
    rebar/host:docker_unaccounted_bytes by observability.sh 2f.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "docker_unaccounted_bytes"
  statistic   = "Maximum"

  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  # 2 GiB. Some divergence between `du` and the ledger is NORMAL — `du` counts allocated
  # blocks including per-layer directory and whiteout overhead plus the daemon's own metadata
  # (image/, network/, buildkit/*.db, tmp/), while the ledger reports layer sizes with sharing
  # accounted differently — so this is deliberately not "any divergence": far above that
  # overhead (hundreds of MB on this box), and far below the 6.5 GB that went unnoticed.
  threshold           = 2147483648
  comparison_operator = "GreaterThanThreshold"

  # The residue needs BOTH the filesystem read and the ledger read to succeed, so silence here
  # means one of them failed — and an unmeasured orphan mass is the state this alarm exists
  # for, not a state it may report as healthy.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "9183"
  }
}

# ---------------------------------------------------------------------------
# journald, the /var/log generator (ADR 0112 decisions 1+2, story e956-b1c3-45b9-4016)
# ---------------------------------------------------------------------------
# The other accumulator the 2026-09-02 measurement found: /var/log was 1.8G of the 28G root
# working set and 1.7G of that was the JOURNAL, because every compose service logs to the host
# journal. rebar-root-disk-pressure could only say "root disk high".
#
# TWO alarms, because neither implies the other — the rebar-gerrit-data-disk-debris (task 3e92)
# argument again:
#
#   1. journal-usage-high        — is the journal approaching the ceiling it is measured against?
#   2. journal-cap-not-in-effect — is that ceiling the one the RUNNING journald actually read?
#                                  journald reads its configuration at startup and
#                                  systemd-journald.service implements no ExecReload, so a
#                                  ceiling can sit installed on disk while the live daemon
#                                  enforces the one it read at boot. In that state alarm 1 is
#                                  computed against a denominator that is NOT in force and
#                                  reads perfectly healthy, which is exactly the gap story 9183
#                                  left open for its BuildKit share.
#
# HONEST STRENGTH, so an operator reading these during an incident is not misled: SystemMaxUse
# is a real cap that journald enforces as it extends a journal file, but only ARCHIVED files are
# vacuumed, so usage can exceed the ceiling by up to one active file (SystemMaxFileSize, default
# 1/8 of the ceiling). And it bounds /var/log/journal ONLY — the rest of /var/log is covered
# only by rebar-root-disk-pressure.
#
# Published dimensionless by observability.sh 2g on the 5-minute cadence, following
# root_disk_used_percent: CloudWatch keys a metric by namespace+name+dimensions, so a dimension
# on only one side silently never matches. The ceiling behind the percent comes from
# infra/scripts/journald-cap.sh, the same file that renders the drop-in journald reads.

resource "aws_cloudwatch_metric_alarm" "journal_usage_high" {
  alarm_name        = "rebar-journal-usage-high"
  alarm_description = <<-EOT
    The systemd journal is above 85% of its configured ceiling (SystemMaxUse; the value lives in
    infra/scripts/journald-cap.sh, readable with `--print-env`). journald will vacuum ARCHIVED
    journal files to stay under the ceiling, so this is a warning that history is about to be
    discarded rather than that the disk is about to fill — but sustained growth here is a
    generator to investigate, because the ceiling is not the whole story: only archived files
    are vacuumed, so usage can exceed it by up to one active file. CHECK
    rebar-journal-cap-not-in-effect FIRST: if that alarm is also firing, this percentage is
    measured against a ceiling the running journald never read. DIAGNOSIS: `journalctl
    --disk-usage`, `du -sx /var/log/journal`. REMEDIATION: `journalctl --vacuum-size=` (never
    `rm` under /var/log/journal). Published as rebar/host:journal_used_percent by
    observability.sh 2g. Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "journal_used_percent"
  statistic   = "Maximum"

  # The house 300/3/2 shape (root_disk_pressure above): 2 breaching datapoints in a 3-period
  # window absorbs the ordinary timer jitter that makes ~2 of 24 periods absent on this box.
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # observability.sh 2g publishes this ONLY on a successful measurement, so absence means the
  # probe could not size the journal — which is exactly when this must page rather than clear to
  # OK (bug 3276 defect 2). Pinned by tests/unit/test_alarm_actions_terraform.py.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "e956"
  }
}

resource "aws_cloudwatch_metric_alarm" "journal_cap_not_in_effect" {
  alarm_name        = "rebar-journal-cap-not-in-effect"
  alarm_description = <<-EOT
    The journal ceiling on disk is NOT the one the running systemd-journald is enforcing.
    journald reads its configuration at STARTUP only and the unit implements no ExecReload, so a
    drop-in installed under a live daemon stays dormant until that daemon restarts. While this
    fires, rebar-journal-usage-high is computed against a denominator that is not in force and
    can read healthy while the journal grows to whatever ceiling the daemon read at boot (or to
    journald's derived default). CONFIRM with `bash infra/scripts/journald-cap.sh
    --check-active` (prints 1/0) and `--install`, which reports the state in words. REMEDIATE by
    restarting the logger: `systemctl restart systemd-journald`. Published as
    rebar/host:journal_cap_in_effect, a 1/0 heartbeat on every probe tick.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "journal_cap_in_effect"
  statistic   = "Minimum"

  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics (the gate_scratch_unmounted shape): the healthy path publishes 1 on
  # EVERY tick, so absence means the probe, the timer, or the host is dead — the one state this
  # alarm most needs to announce.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "e956"
  }
}

# --- /var/tmp, the fourth root generator (ADR 0112 / story 2ba3-bf77-1303-4b2d) --------
# Published dimensionless by observability.sh 2h on the 5-minute cadence, following
# root_disk_used_percent. The budget behind the percent comes from infra/scripts/vartmp-cap.sh,
# the same file that renders the tmpfiles drop-in and the reaper units.
#
# TWO alarms, not three. `var_tmp_hard_quota_in_effect` is published beside these and is
# deliberately NOT alarmed: the XFS project quota it reports needs `rootflags=pquota` and a host
# reboot, so its honest value is 0 until that reboot is scheduled, and an alarm that pages
# continuously is muted within a day. It is a capacity fact to read when interpreting
# rebar-var-tmp-usage-high, not an incident.

resource "aws_cloudwatch_metric_alarm" "var_tmp_usage_high" {
  alarm_name        = "rebar-var-tmp-usage-high"
  alarm_description = <<-EOT
    /var/tmp is above 85% of its configured byte budget (the value lives in
    infra/scripts/vartmp-cap.sh, readable with `--print-env`). READ THIS DIFFERENTLY FROM THE
    OTHER GENERATOR ALARMS: unless rebar/host:var_tmp_hard_quota_in_effect is 1, nothing
    ENFORCES this number — /var/tmp is a directory on the root XFS filesystem with no
    writer-side cap, and the budget is held by rebar-var-tmp-reaper.timer, which evicts
    oldest-first every 5 minutes and can be outrun by a fast writer. So this can be the
    early warning it looks like, or it can already be a root-volume incident in progress; check
    rebar-root-disk-pressure alongside it. DIAGNOSIS: `du -sx --block-size=1 /var/tmp`, then
    `du -sh /var/tmp/* | sort -h | tail` to name the generator. REMEDIATION: delete the
    generator's own scratch, or run `bash infra/scripts/vartmp-cap.sh --reap` to force a pass.
    Published as rebar/host:var_tmp_used_percent by observability.sh 2h.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "var_tmp_used_percent"
  statistic   = "Maximum"

  # The house 300/3/2 shape (root_disk_pressure above): 2 breaching datapoints in a 3-period
  # window absorbs the ordinary timer jitter that makes ~2 of 24 periods absent on this box.
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # observability.sh 2h publishes this ONLY on a successful measurement, so absence means the
  # probe could not size /var/tmp — which is exactly when this must page rather than clear to OK
  # (bug 3276 defect 2). Pinned by tests/unit/test_alarm_actions_terraform.py.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "2ba3"
  }
}

resource "aws_cloudwatch_metric_alarm" "var_tmp_cleanup_not_active" {
  alarm_name        = "rebar-var-tmp-cleanup-not-active"
  alarm_description = <<-EOT
    NOTHING is bounding /var/tmp. Either the tmpfiles drop-in is missing or no longer matches
    what infra/scripts/vartmp-cap.sh renders, or rebar-var-tmp-reaper.timer is not running — and
    with no XFS project quota in force (rebar/host:var_tmp_hard_quota_in_effect) that leaves the
    tree bounded only by the size of the root volume, which is the 2026-09-02 outage exactly.
    While this fires, rebar-var-tmp-usage-high is measured against a budget nothing is holding.
    CONFIRM with `bash infra/scripts/vartmp-cap.sh --check-active` (prints 1/0). REMEDIATE with
    `bash infra/scripts/vartmp-cap.sh --install`, which is idempotent and reports the state in
    words, then `systemctl status rebar-var-tmp-reaper.timer`. Published as
    rebar/host:var_tmp_cleanup_active, a 1/0 heartbeat on every probe tick.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "var_tmp_cleanup_active"
  statistic   = "Minimum"

  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics (the gate_scratch_unmounted / journal_cap_not_in_effect shape): the
  # healthy path publishes 1 on EVERY tick, so absence means the probe, the timer, or the host is
  # dead — the one state this alarm most needs to announce.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "2ba3"
  }
}

# --- writable CONTAINER layers, the last root generator (ADR 0112 / story 910b-2d43-4482-4c64) ---
# Published dimensionless by observability.sh 2i on the 5-minute cadence, following
# root_disk_used_percent. The share behind the percent comes from infra/scripts/container-cap.sh,
# which reads it from docker-storage-cap.sh — ONE Docker budget with an internal split, never a
# second cap over the same overlay2 bytes.
#
# TWO alarms, not three. `container_quota_enforceable` is published beside these and is
# deliberately NOT alarmed: overlay2's per-container `--storage-opt size=` needs XFS with the
# pquota mount option on the filesystem backing /var/lib/docker, which on this root filesystem
# needs rootflags=pquota and a host reboot — so its honest value is 0 until that reboot is
# scheduled, and an alarm that pages continuously is muted within a day. It is a capacity fact to
# read when interpreting rebar-container-writable-usage-high, not an incident.

resource "aws_cloudwatch_metric_alarm" "container_writable_usage_high" {
  alarm_name        = "rebar-container-writable-usage-high"
  alarm_description = <<-EOT
    Writable container layers are above 85% of their share of the Docker budget (the value lives
    in infra/scripts/container-cap.sh, readable with `--print-env`). READ THIS KNOWING WHAT HOLDS
    IT: rebar-container-reaper.timer can only remove EXITED containers, so if the bytes belong to
    a RUNNING service NOTHING here bounds them — only an overlay2 per-container quota would, and
    rebar/host:container_quota_enforceable says whether this host can even have one. DIAGNOSIS:
    `docker ps -a --size` names the containers; compare rebar/host:container_exited_bytes against
    container_writable_bytes to see whether this is reclaimable debris or the live services.
    REMEDIATION: `bash infra/scripts/container-cap.sh --reap` forces a pass; a live service
    growing its own layer is an application problem, not a cleanup one. NEVER delete anything
    under /var/lib/docker/overlay2 by hand. Published as
    rebar/host:container_writable_used_percent by observability.sh 2i.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "container_writable_used_percent"
  statistic   = "Maximum"

  # The house 300/3/2 shape (root_disk_pressure above): 2 breaching datapoints in a 3-period
  # window absorbs the ordinary timer jitter that makes ~2 of 24 periods absent on this box.
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # observability.sh 2i publishes this ONLY when `docker system df` yielded a parseable Containers
  # row, so absence means the probe could not size writable layers — exactly when this must page
  # rather than clear to OK (bug 3276 defect 2). Pinned by tests/unit/test_alarm_actions_terraform.py.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "910b"
  }
}

resource "aws_cloudwatch_metric_alarm" "container_reaper_not_active" {
  alarm_name        = "rebar-container-reaper-not-active"
  alarm_description = <<-EOT
    NOTHING is reaping exited-container debris. Either rebar-container-reaper.timer is not
    running or its units no longer match what infra/scripts/container-cap.sh renders — and since
    no per-container overlay2 quota is in force on this host (see
    rebar/host:container_quota_enforceable), that leaves writable layers bounded only by the size
    of the root volume, which is the 2026-09-02 outage exactly. While this fires,
    rebar-container-writable-usage-high is measured against a share nothing is holding. CONFIRM
    with `bash infra/scripts/container-cap.sh --check-active` (prints 1/0). REMEDIATE with
    `bash infra/scripts/container-cap.sh --install`, which is idempotent and reports the state in
    words, then `systemctl status rebar-container-reaper.timer`. Published as
    rebar/host:container_reaper_active, a 1/0 heartbeat on every probe tick.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "container_reaper_active"
  statistic   = "Minimum"

  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics (the var_tmp_cleanup_not_active / journal_cap_not_in_effect shape): the
  # healthy path publishes 1 on EVERY tick, so absence means the probe, the timer, or the host is
  # dead — the one state this alarm most needs to announce.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "910b"
  }
}
