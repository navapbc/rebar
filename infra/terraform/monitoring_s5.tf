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

  # 5-minute periods; alarm on a single period with any failure lines.
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # No replication failures published in a period is the healthy steady state and
  # should NOT alarm, so missing data is treated as not-breaching.
  treat_missing_data = "notBreaching"

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
