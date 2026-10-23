# The GitHub Actions bridge publishes dimensionless
# rebar/host:bridge_run_failures, including healthy zero. The host probe cannot
# observe this workflow. Two consecutive hourly failures page through SNS,
# while one transient miss does not. Missing data is notBreaching because a
# stopped workflow is owned by the Reconciler Heartbeat Canary. This alarm
# watches runs that execute and fail.

resource "aws_cloudwatch_metric_alarm" "bridge_run_failures" {
  alarm_name        = "rebar-bridge-run-failures"
  alarm_description = <<-EOT
    The Reconcile Bridge (GitHub Actions, .github/workflows/reconcile-bridge.yml) failed in TWO
    CONSECUTIVE hours, published as rebar/host:bridge_run_failures (1 per failed run, 0 per
    healthy run). MEANING: the Jira<->rebar reconciler is not converging, and ticket state drifts
    silently for as long as it stays down. The archetype is the 2026-08-12 JIRA_API_TOKEN expiry:
    hours of failure with no CloudWatch signal at all before this metric existed. REMEDIATION:
    open the newest Reconcile Bridge workflow run and read its failing step, then check
    JIRA_API_TOKEN validity (expiry is the most common cause) plus the JIRA_URL / JIRA_USER
    repo Variables. A SINGLE failed pass deliberately does not alarm — that is usually a
    transient API blip the next hourly pass heals; a healthy pass publishes 0 and breaks the
    streak. If the workflow has stopped running
    altogether this alarm stays OK by design; the Reconciler Heartbeat Canary owns that case.
  EOT

  namespace   = "rebar/host"
  metric_name = "bridge_run_failures"
  statistic   = "Sum"

  # See the cadence rationale above: a failed pass in each of two CONSECUTIVE hours. At the
  # hourly bridge cadence a "2 failures within one hour" threshold could never be reached.
  period              = 3600
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # rebar:allow-missing-data-notbreaching: the publisher is the GitHub Actions bridge
  # workflow, not the host probe, so it cannot emit a healthy 0 for a run that never
  # happened; the stopped-entirely case is owned by the Reconciler Heartbeat Canary. See
  # the cadence rationale at the top of this file (ticket bff5-9163-cddd-4158).
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "58e0"
  }
}
