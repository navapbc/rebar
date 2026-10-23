# Compare Gerrit and GitHub main SHAs through the host probe, publishing
# dimensionless rebar/host:mirror_out_of_sync (1 diverged, 0 equal).
# This catches silent replication stalls with no replication_errors log line.
# Sustained divergence alerts the shared SNS topic.

resource "aws_cloudwatch_metric_alarm" "mirror_out_of_sync" {
  alarm_name        = "rebar-gerrit-mirror-out-of-sync"
  alarm_description = <<-EOT
    GitHub `main` has diverged from Gerrit `main` for a sustained window — Gerrit
    is the source of truth and replication should keep GitHub in lockstep, so a
    persistent divergence means replication is stuck/failing and the mirror is
    stale. Published as the custom metric rebar/host:mirror_out_of_sync (1=diverged)
    by the host observability probe (observability.sh section 5). See
    infra/runbooks/github-mirror-lock.md for the replication-failure rollback trigger.
  EOT

  namespace   = "rebar/host"
  metric_name = "mirror_out_of_sync"
  statistic   = "Maximum" # the flag is 1/0; alarm if it is 1 across the window

  # PROFILE A: 8-of-8 over 40 minutes. One matching SHA reading clears it.
  # A 2-of-3 window falsely paged on brief replication lag plus an empty bucket.
  # Persistent divergence can make CI test a different tree than Gerrit's main.
  period              = 300
  evaluation_periods  = 8
  datapoints_to_alarm = 8
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # Section 5 publishes 0 for matching SHAs and 1 for divergence or comparison
  # failure. Silence breaches. This last probe metric is the tail sentinel for
  # nine earlier error counters that use notBreaching. Do not move its publish
  # earlier or add a counter after it. test_alarm_window_tuning guards that order.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "WS7-a774"
  }
}
