# The host probe counts Gerrit replication_log failure markers, including
# rejected non-fast-forward and exhausted retries, as dimensionless
# rebar/host:replication_errors. Notify shared SNS on sustained failures.
# This catches logged errors. mirror_out_of_sync checks the main SHAs.

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

  # PROFILE B: two observed error periods within four catch intermittent failures.
  # Missing buckets do not count as errors. Treating missing data as breaching would page on
  # ordinary gaps while the counter still reports zero.
  period              = 300
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # PROFILE B: observed errors page. Missing data alone does not.
  # rebar:allow-missing-data-notbreaching: liveness is carried by the dead-man alarms, which keep
  # treat_missing_data = "breaching". The configured window catches observed
  # intermittent errors without false pages when the probe misses a zero.
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
