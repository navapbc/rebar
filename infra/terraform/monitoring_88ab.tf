# ---------------------------------------------------------------------------
# monitoring_88ab.tf — CloudWatch alarm for review-bot MERGE-CHANGE path failures.
# Epic 88ab / S2 (urge-brook-plume).
# ---------------------------------------------------------------------------
# METRIC SOURCE: the review-bot reviews a merge change on ONLY its auto-merge delta
# (get_merge_files / get_file_diff / get_mergelist — never the bare /patch, which 409s
# on a merge). When any of those merge-path REST calls fails, the voter writes a
# structured `MERGE_CHANGE_ERROR` marker to stderr (rebar.review_bot.voter.
# _merge_change_error) and fails closed. The HOST observability probe
# (infra/scripts/observability.sh §4c) greps the review-bot container's journald for
# those markers since the last run and publishes a per-period count to the custom
# metric rebar/host:review_bot_merge_change_errors — the same host-grep pattern S4b
# uses for voter_errors. This alarm watches THAT metric.
#
# WHY A SEPARATE METRIC (not just voter_errors): a merge-path failure ALSO increments
# voter_errors (the voter fails closed, so the aggregate health metric already catches
# it). This metric is the GRANULAR signal — it isolates "the merge-change path
# specifically is broken" (e.g. a Gerrit upgrade changed the files/ auto-merge default,
# or the mergelist endpoint regressed) from general voter failure, so an operator can
# tell a feature-branch-flow regression from an unrelated voter outage. The two are
# deliberately double-counted across two different metrics answering two questions.
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = review_bot_merge_change_errors
#   Dimensions = NONE — DIMENSIONLESS ON BOTH SIDES (see monitoring_s4b.tf for the
#                  keying rationale; change BOTH the probe and this alarm or neither).
#   Unit       = Count   (a per-period count of new MERGE_CHANGE_ERROR log lines)
#
# ACTION: unlike the S4b voter_errors alarm (which is metric-only), this alarm WIRES the
# shared SNS alerts topic (aws_sns_topic.alerts, monitoring.tf) on both alarm and OK
# transitions — an alarm without an action fires silently. Mirrors the WS7 / 1fa8 alarms.
# ---------------------------------------------------------------------------

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

  # 5-minute periods; alarm on a single period with any merge-change-error lines.
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # No merge-change errors in a period is the healthy steady state and must NOT alarm.
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
