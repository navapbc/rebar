# ---------------------------------------------------------------------------
# monitoring_ws7.tf — CloudWatch alarm for Gerrit -> GitHub mirror out-of-sync.
# Ticket a774 (epic b744), post-WS7 cutover.
# ---------------------------------------------------------------------------
# WHY THIS IS DISTINCT FROM THE S5 replication_errors ALARM: S5 counts FAILURE
# LINES in the replication_log. But a replication that silently stops firing (or a
# push that is never attempted) logs no failure, so S5 stays green while GitHub
# `main` quietly falls behind Gerrit `main`. This alarm watches the ACTUAL end
# state: are the two `main` SHAs equal? The host probe (observability.sh section 5)
# compares the anonymous Gerrit REST revision against a public `git ls-remote` of
# GitHub and publishes mirror_out_of_sync = 1 (diverged) / 0 (in sync).
#
# Custom metric contract (what the host probe PutMetricData's):
#   Namespace  = rebar/host
#   MetricName = mirror_out_of_sync
#   Dimensions = NONE — DIMENSIONLESS ON BOTH SIDES (same rule as S5: CloudWatch keys
#                  a metric by namespace+name+dimensions; change BOTH sides or neither).
#   Unit       = Count   (1 = diverged, 0 = in sync)
#
# Reuses var.aws_region + data.aws_caller_identity.current (iam.tf) and the shared
# aws_sns_topic.alerts (monitoring.tf). Unlike the S5/S4b alarms, this one WIRES SNS
# (a774 requires an actual alert): a sustained mirror divergence pages the operator.
# ---------------------------------------------------------------------------

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

  # Require a SUSTAINED divergence (2 x 5-min periods = ~10 min) so the normal
  # ~15s post-submit replication lag never pages — only a genuinely stuck mirror does.
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # Missing data (probe not publishing / a transient fetch failure that publishes
  # nothing) is treated as not-breaching, matching S5 — the probe fails safe.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "WS7-a774"
  }
}
