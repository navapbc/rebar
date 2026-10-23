# Merge-change REST failures write MERGE_CHANGE_ERROR. The host probe counts
# them as dimensionless rebar/host:review_bot_merge_change_errors. The same
# failure also increments aggregate voter_errors. This metric isolates the
# merge path. Notify the shared SNS topic on alarm and recovery.

resource "aws_cloudwatch_metric_alarm" "review_bot_merge_change_errors" {
  alarm_name        = "rebar-review-bot-merge-change-errors"
  alarm_description = <<-EOT
    rebar review-bot MERGE-CHANGE review-path failures detected in the receiver's
    journald (MERGE_CHANGE_ERROR markers: a get_merge_files / get_mergelist / per-file
    get_file_diff REST call failed). Published as rebar/host:review_bot_merge_change_errors
    by the host observability probe (§4c). The voter fails closed, so the merge change is
    left unsubmittable; a persistent signal here means the feature-branch merge-review path
    is broken (e.g. a Gerrit REST change to the files/ auto-merge default) — investigate the
    merge-path client (src/rebar/review_bot/gerrit_client.py) against the running Gerrit.
  EOT

  namespace   = "rebar/host"
  metric_name = "review_bot_merge_change_errors"
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

  # WIRE the shared alerts topic so the alarm is not silent (unlike S4b's metric-only
  # alarm). Reuses aws_sns_topic.alerts from monitoring.tf (see WS7 / 1fa8 alarms).
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Epic    = "88ab"
    Story   = "S2"
  }
}
