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
# Root-filesystem disk pressure (incident 2731). The box's 30G ROOT disk holds
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
    The rebar box's ROOT filesystem is above 85% used. Docker image/build-cache
    storage and the review-bot's clone tmp live here: exhaustion fail-closes every
    LLM-Review vote (incident 2731). Reclaim with the autodeploy prune helper /
    `docker builder prune` and check /tmp/rebar-gate-snapshots; published as
    rebar/host:root_disk_used_percent by observability.sh §2 (5-min cadence).
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
