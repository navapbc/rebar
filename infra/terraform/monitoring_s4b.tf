# The host probe counts review-bot VOTER_ERROR journal markers as
# dimensionless rebar/host:voter_errors. A missing LLM-Review vote blocks
# submission. Notify the shared SNS topic when voting stays broken.
# Keep publisher and alarm namespace/name/dimensions identical.

resource "aws_cloudwatch_metric_alarm" "voter_errors" {
  alarm_name        = "rebar-gerrit-voter-errors"
  alarm_description = <<-EOT
    SUSTAINED rebar review-bot LLM-Review voter failure detected in the receiver's
    journald log (VOTER_ERROR markers: Gerrit 4xx/5xx, clone/diff failure, LLM
    unavailable, or an expired bot token). Published as the custom metric
    rebar/host:voter_errors by the host observability probe. Because submit requires
    the LLM-Review vote (ADR-0013), a failing voter leaves changes unsubmittable —
    the fail-closed gate. Restore the voter (token / LLM / receiver); there is no
    break-glass to disable the submit requirement. Calibrated to sustained-failure
    detection (ticket ea5d-4932-8554-4544): 3 of 6 five-minute periods with any
    markers — an isolated self-healing transient (timeout->backfill retry, clone
    stall, retry-budget fail-closed -1) never pages, even straddling a period
    boundary; a stuck gate emits markers on every retry (~5-min reconciler cadence)
    and pages in ~15 minutes.
  EOT

  namespace   = "rebar/host"
  metric_name = "voter_errors"
  statistic   = "Sum"

  # PROFILE B: 3 observed error periods within six 5-minute periods page.
  # A transient spanning 1-2 periods stays quiet. A stuck voter retries about
  # every 5 minutes. The N-of-M window catches phase-shifted intermittent
  # failures while missing buckets no longer count as voter errors.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # PROFILE B: observed errors page. Missing data alone does not.
  # rebar:allow-missing-data-notbreaching: liveness is carried by the dead-man alarms, which keep
  # treat_missing_data = "breaching". The configured window catches observed
  # intermittent errors without false pages when the probe misses a zero.
  treat_missing_data = "notBreaching"

  # Both alarm and recovery notify SNS. A failed LLM voter blocks Submit.
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "S4b"
  }
}

# Discoverability anchor: the alarm's metric is account/region-scoped under this
# identity (mirrors monitoring_s5.tf's locals; makes region/account provenance
# explicit and matches iam.tf's usage).
locals {
  voter_alarm_region     = var.aws_region
  voter_alarm_account_id = data.aws_caller_identity.current.account_id
}
