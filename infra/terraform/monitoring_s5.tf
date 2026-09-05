# ---------------------------------------------------------------------------
# monitoring_s5.tf — CloudWatch alarm for Gerrit -> GitHub replication failures.
# Story S5.
# ---------------------------------------------------------------------------
# METRIC SOURCE: Gerrit's container stdout/stderr ships to journald (S2), and the
# `replication` plugin writes a structured `replication_log` under the site logs
# dir. There is no native CloudWatch metric for replication health, and wiring a
# CloudWatch Logs metric filter off journald is heavy (it requires shipping the
# journal into a CloudWatch Logs group first). The lighter, committed approach
# used here: a HOST log-watcher (the S2/S7 observability probe) greps the
# replication_log for failure signatures — `ERROR`, `REJECTED_NONFASTFORWARD`,
# and the max-retry/"giving up" lines the plugin emits when replicationMaxRetries
# is exhausted — and publishes a custom CloudWatch metric. This alarm watches
# THAT metric. If the probe is not yet publishing the metric, the alarm sits in
# INSUFFICIENT_DATA (treated as not-breaching here) rather than firing falsely.
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = replication_errors
#   Dimensions = NONE — DIMENSIONLESS ON BOTH SIDES. The probe publishes with no
#                  dimensions and this alarm declares none. CloudWatch keys a metric
#                  by namespace+name+dimensions, so adding a dimension to ONLY one
#                  side makes the alarm silently stop matching. Change BOTH or neither.
#   Unit       = Count   (a per-period count of new failure log lines)
#
# Reuses var.aws_region and data.aws_caller_identity.current (declared in iam.tf)
# for ARNs, matching the repo's existing patterns.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "replication_errors" {
  alarm_name        = "rebar-gerrit-replication-errors"
  alarm_description = <<-EOT
    Gerrit -> GitHub replication failures detected in the replication_log
    (ERROR / REJECTED_NONFASTFORWARD / max-retry exhausted). Published as the
    custom metric rebar/host:replication_errors by the host observability probe.
    A non-fast-forward rejection means GitHub history diverged from Gerrit — the
    one-way-door contract (ADR-0010) was violated and needs operator attention.
  EOT

  namespace   = "rebar/host"
  metric_name = "replication_errors"
  statistic   = "Sum"

  # PROFILE B (error counter), 2-of-4 over 20 minutes — infra/runbooks/alarm-window-tuning.md.
  #
  # 2-of-3 was the shape ticket a9d1-c7f3-cfd9-44ff was opened for: with treat_missing_data =
  # "breaching" (below) an empty period IS a breaching datapoint, so two ordinary scheduling
  # gaps satisfied datapoints_to_alarm on their own, with the counter reading 0 throughout.
  # Observed 2026-09-05: four counters in ALARM at once, each StateReason reading "1 datapoint was
  # received for 3 periods and 2 missing datapoints were treated as [Breaching]".
  #
  # datapoints_to_alarm is UNCHANGED at 2, deliberately. This alarm must keep catching an
  # INTERMITTENT error stream — one that is not phase-aligned with CloudWatch period boundaries
  # and lands markers in one period and none in the next — which an N-of-N streak would reset on.
  # So the M < N sensitivity stays and the missing-data rule changes instead (below). Only
  # evaluation_periods widens, to 4, so that 2 REAL datapoints are reachable inside the window
  # at the publisher's contractual 5-9 minute inter-arrival (install-observability.sh:
  # OnUnitActiveSec=5min measured from the last COMPLETED run, plus TimeoutStartSec=240). An
  # alarm that cannot fire is as useless as one that always fires.
  period              = 300
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # PROFILE B, ticket a9d1-c7f3-cfd9-44ff: absence of a delta report is not evidence of errors.
  # rebar:allow-missing-data-notbreaching: liveness is carried by the dead-man alarms, which keep
  # treat_missing_data = "breaching" with datapoints_to_alarm == evaluation_periods and span
  # observability.sh from §1 (GerritReachable) and §1b (mcp_healthy) through §2e/§2g/§2h/§2i (the
  # mount / journal-cap / var-tmp-cleanup / container-reaper heartbeats) to §5 (mirror_out_of_sync).
  # A stopped probe, and a run truncated by the unit's 240s TimeoutStartSec, therefore still page —
  # ONCE, with an accurate message, instead of once per counter with a false one. bff5's premise
  # (this counter publishes 0 unconditionally, so silence means a dead publisher) is still true;
  # what bff5 got wrong was making EVERY counter say so, which is what turned one gap into a
  # multi-alarm page. This is the conflation of liveness with condition the ticket names.
  treat_missing_data = "notBreaching"

  # Notify the shared alerts topic on BOTH edges (ticket 9baf). Like S4b's voter_errors, this
  # alarm declared neither and so notified nobody. A non-fast-forward rejection means GitHub
  # history diverged from Gerrit — a violation of the one-way-door contract (ADR-0010) that
  # explicitly "needs operator attention" per this alarm's own description above, which an
  # actionless alarm cannot deliver.
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "S5"
  }
}

# Discoverability anchor: the alarm's metric is account/region-scoped under this
# identity (no cross-account ARN is constructed, but the references make the
# region/account provenance explicit and match iam.tf's usage).
locals {
  replication_alarm_region     = var.aws_region
  replication_alarm_account_id = data.aws_caller_identity.current.account_id
}
