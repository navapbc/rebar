# ---------------------------------------------------------------------------
# monitoring_autolander.tf — CloudWatch alarm for auto-lander failures.
# Epic f1fa / story S5.
# ---------------------------------------------------------------------------
# METRIC SOURCE: the serial auto-lander (compose-autolander-1) writes AUTOLANDER_ERROR
# (a landing step failed) and AUTOLANDER_HANDBACK (it handed a stack back rather than land
# it) markers to stdout -> journald, and its heartbeat HEALTHCHECK flips the container
# `unhealthy` when the loop stops heartbeating (wedged/deadlocked). The HOST observability
# probe (infra/scripts/observability.sh §4e) greps the container's journald for those
# markers since the last run AND adds the Docker `unhealthy` health-state, publishing the
# summed per-period count to the custom metric rebar/host:autolander_errors — the same
# host-grep pattern S4b uses for voter_errors. This alarm watches THAT metric.
#
# WHY THIS MATTERS: the auto-lander is the only actor that lands Autosubmit-requested
# changes; a persistently failing or wedged loop silently stalls landing (changes sit
# submittable-but-unlanded). The alarm surfaces that to an operator instead of letting the
# landing queue back up unnoticed. The two-vote submit gate is unaffected — the lander only
# actuates a land the gate already permits.
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = autolander_errors
#   Dimensions = NONE — DIMENSIONLESS ON BOTH SIDES (see monitoring_s4b.tf for the keying
#                  rationale; change BOTH the probe and this alarm or neither).
#   Unit       = Count  (per-period count of new AUTOLANDER_ERROR/HANDBACK lines, plus 1
#                  when the container health-state is `unhealthy`)
#
# ACTION: wires the shared SNS alerts topic (aws_sns_topic.alerts, monitoring.tf) on both
# alarm and OK transitions — an alarm without an action fires silently. Mirrors the 88ab /
# WS7 / 1fa8 alarms.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "autolander_errors" {
  alarm_name        = "rebar-autolander-errors"
  alarm_description = <<-EOT
    rebar auto-lander failures detected: AUTOLANDER_ERROR / AUTOLANDER_HANDBACK markers in
    the container's journald, or the container reporting `unhealthy` (its heartbeat
    HEALTHCHECK stopped passing — a wedged/deadlocked loop). Published as the custom metric
    rebar/host:autolander_errors by the host observability probe (§4e). A persistent signal
    means Autosubmit-requested changes are not landing — investigate the loop
    (infra/autolander/loop.py) and the container health (docker inspect compose-autolander-1).
  EOT

  namespace   = "rebar/host"
  metric_name = "autolander_errors"
  statistic   = "Sum"

  # 5-minute periods; alarm on a single period with any auto-lander errors/unhealthy signal.
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # No auto-lander errors in a period is the healthy steady state and must NOT alarm.
  treat_missing_data = "notBreaching"

  # WIRE the shared alerts topic so the alarm is not silent. Reuses aws_sns_topic.alerts
  # from monitoring.tf (see 88ab / WS7 / 1fa8 alarms).
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Epic    = "f1fa"
    Story   = "S5"
  }
}
