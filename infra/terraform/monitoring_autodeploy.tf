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

  treat_missing_data = "notBreaching"

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
# that would interrupt a review; this alarm watches the cases where it recreated anyway — the
# DEPLOY_DEFER_MAX bound was exhausted (a chronically busy bot), or the /health in_flight signal
# was unreadable so the deploy ran blind. Routine, healthy deferrals are a SEPARATE metric
# (deploy_deferrals) and deliberately do not page.
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = review_interrupts
#   Dimensions = NONE — dimensionless on BOTH sides (see monitoring_s4b.tf rationale).
#   Unit       = Count  (per-period count of new AUTODEPLOY_REVIEW_INTERRUPT journal lines)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "review_interrupts" {
  alarm_name        = "rebar-autodeploy-review-interrupts"
  alarm_description = <<-EOT
    A rebar auto-deploy recreated the review-bot container while a review was in flight, so an
    LLM-Review was killed (AUTODEPLOY_REVIEW_INTERRUPT in the rebar-autodeploy.service journal:
    either the DEPLOY_DEFER_MAX deferral bound was exhausted, or /health's in_flight signal was
    unreadable and the deploy proceeded without a drain check). Published as
    rebar/host:review_interrupts by the host observability probe (4e). Interrupted reviews are
    retried by the backfill reconciler, so this is not itself an outage — but it is the ONLY
    signal for a failure mode that leaves voter_errors, restarts and the deploy log all green,
    and repeated interruptions live-lock the gate: changes reach `Verified +1` with
    `LLM-Review = 0` and cannot submit. Investigate `journalctl -u rebar-autodeploy` for the
    marker's reason field: `bound-exceeded` means the bot is chronically busy (reviews are
    queueing faster than they complete), `signal-unavailable` means the drain check itself is
    broken and every deploy is now running blind.
  EOT

  namespace   = "rebar/host"
  metric_name = "review_interrupts"
  statistic   = "Sum"

  # Cadence matches the deferral bound (autodeploy.sh DEPLOY_DEFER_MAX=1800s): a
  # bound-exceeded interrupt cannot recur faster than one per episode, so a single 900s
  # period > 0 is enough to latch, and `signal-unavailable` recurs on EVERY tick while the
  # probe is broken. One datapoint keeps the "deploys are running blind" case from waiting
  # an hour to surface — the blindness is the thing being alarmed.
  period              = 900
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "notBreaching"

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

  # A dead probe/host is caught by the S7 gate-down alarm (treat_missing_data =
  # breaching there); duplicating that here would double-page on host loss.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "ac14"
  }
}
