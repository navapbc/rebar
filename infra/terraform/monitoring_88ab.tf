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

  # PROFILE B (error counter), 2-of-4 over 20 minutes — infra/runbooks/alarm-window-tuning.md.
  #
  # 2-of-3 was the shape ticket a9d1-c7f3-cfd9-44ff was opened for: with treat_missing_data =
  # "breaching" (below) an empty period IS a breaching datapoint, so two ordinary scheduling
  # gaps satisfied datapoints_to_alarm on their own, with the counter reading 0 throughout.
  # Observed 2026-09-05: four counters in ALARM at once, each StateReason reading "1 datapoint was
  # received for 3 periods and 2 missing datapoints were treated as [Breaching]".
  #
  # datapoints_to_alarm is UNCHANGED at 2, deliberately. This alarm must keep catching an
  # INTERMITTENT error stream — one that is not phase-aligned with CloudWatch period boundaries
  # and lands markers in one period and none in the next — which an N-of-N streak would reset on.
  # So the M < N sensitivity stays and the missing-data rule changes instead (below). Only
  # evaluation_periods widens, to 4, so that 2 REAL datapoints are reachable inside the window
  # at the publisher's contractual 5-9 minute inter-arrival (install-observability.sh:
  # OnUnitActiveSec=5min measured from the last COMPLETED run, plus TimeoutStartSec=240). An
  # alarm that cannot fire is as useless as one that always fires.
  period              = 300
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # PROFILE B, ticket a9d1-c7f3-cfd9-44ff: absence of a delta report is not evidence of errors.
  # rebar:allow-missing-data-notbreaching: liveness is carried by the dead-man alarms, which keep
  # treat_missing_data = "breaching" with datapoints_to_alarm == evaluation_periods and span
  # observability.sh from §1 (GerritReachable) and §1b (mcp_healthy) through §2e/§2g/§2h/§2i (the
  # mount / journal-cap / var-tmp-cleanup / container-reaper heartbeats) to §5 (mirror_out_of_sync).
  # A stopped probe, and a run truncated by the unit's 240s TimeoutStartSec, therefore still page —
  # ONCE, with an accurate message, instead of once per counter with a false one. bff5's premise
  # (this counter publishes 0 unconditionally, so silence means a dead publisher) is still true;
  # what bff5 got wrong was making EVERY counter say so, which is what turned one gap into a
  # multi-alarm page. This is the conflation of liveness with condition the ticket names.
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
