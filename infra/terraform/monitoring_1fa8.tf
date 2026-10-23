# The host probe counts g2p dispatcher errors in Gerrit journald as the
# dimensionless rebar/host:g2p_dispatch_errors metric. A dispatch failure
# prevents the GitHub run and Verified vote, so the two-vote gate fails closed.
# The vote-back leg lives in GitHub Actions, outside this host metric. Notify
# the shared SNS topic. Match probe namespace/name/dimensions exactly.

resource "aws_cloudwatch_metric_alarm" "g2p_dispatch_errors" {
  alarm_name        = "rebar-gerrit-g2p-dispatch-errors"
  alarm_description = <<-EOT
    gerrit-to-platform CI-dispatch failures detected in the Gerrit container's
    journald (g2p error markers: a failed workflow_dispatch, GitHub 4xx/5xx, an
    expired PAT, or a g2p exception on patchset-created / recheck). Published as the
    custom metric rebar/host:g2p_dispatch_errors by the host observability probe
    (observability.sh §6). Once the Verified submit requirement is active, submit
    requires the CI Verified vote — a failing dispatcher leaves changes unsubmittable
    (the fail-closed gate). Fix the dispatcher (PAT / g2p / hooks plugin); the
    temporary back-out to single-vote gating is in
    infra/runbooks/two-vote-gate-rollback.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "g2p_dispatch_errors"
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

  # WIRE SNS (unlike S5/S4b): a broken CI dispatcher blocks all submits once the gate
  # is active, so page the operator (same choice as monitoring_ws7.tf).
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "1fa8-S6"
  }
}

# Discoverability anchor: the alarm's metric is account/region-scoped under this
# identity (mirrors the locals in monitoring_s5.tf / monitoring_s4b.tf).
locals {
  g2p_alarm_region     = var.aws_region
  g2p_alarm_account_id = data.aws_caller_identity.current.account_id
}
