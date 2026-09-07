# ---------------------------------------------------------------------------
# monitoring_4b6e.tf — CloudWatch alarms for host probe liveness heartbeats.
# Bug 4b6e-1090-f339-48f8 (enzymatic-revivable-pheasant).
# ---------------------------------------------------------------------------
# These metrics publish 1 for a successful check and 0 for a failed check on every
# observability.sh tick. Missing data is also unhealthy: it means the timer, host,
# or probe stopped before the health datapoint was written. The windows mirror the
# neighboring Profile A dead-man alarms: 6-of-6 five-minute periods with missing
# data breaching, so a single ordinary publish gap does not page but a sustained
# unhealthy/missing signal does.

resource "aws_cloudwatch_metric_alarm" "gerrit_healthy_down" {
  alarm_name        = "rebar-gerrit-healthy-down"
  alarm_description = "Gerrit /config/server/version is not returning 200, or the host probe stopped publishing gerrit_healthy. Fires after 30 minutes of sustained 0/missing datapoints."

  namespace   = "rebar/host"
  metric_name = "gerrit_healthy"
  statistic   = "Minimum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
  }

  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "4b6e-1090-f339-48f8"
  }
}

resource "aws_cloudwatch_metric_alarm" "reviewbot_healthy_down" {
  alarm_name        = "rebar-reviewbot-healthy-down"
  alarm_description = "Review-bot /review/health is not returning 200, or the host probe stopped publishing reviewbot_healthy. Fires after 30 minutes of sustained 0/missing datapoints."

  namespace   = "rebar/host"
  metric_name = "reviewbot_healthy"
  statistic   = "Minimum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
  }

  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "4b6e-1090-f339-48f8"
  }
}

resource "aws_cloudwatch_metric_alarm" "mem_probe_not_ok" {
  alarm_name        = "rebar-memory-probe-not-ok"
  alarm_description = "The memory probe failed or stopped publishing mem_probe_ok. Memory gauges may be pessimistic/synthesized while this fires; investigate observability.sh and the host."

  namespace   = "rebar/host"
  metric_name = "mem_probe_ok"
  statistic   = "Minimum"

  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "4b6e-1090-f339-48f8"
  }
}

resource "aws_cloudwatch_metric_alarm" "container_stats_probe_not_ok" {
  alarm_name        = "rebar-container-stats-probe-not-ok"
  alarm_description = "The container stats census failed or stopped publishing container_stats_ok. Per-container RSS metrics may be stale while this fires; investigate docker stats."

  namespace   = "rebar/host"
  metric_name = "container_stats_ok"
  statistic   = "Minimum"

  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "4b6e-1090-f339-48f8"
  }
}
