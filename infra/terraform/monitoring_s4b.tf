# ---------------------------------------------------------------------------
# monitoring_s4b.tf — CloudWatch alarm for review-bot LLM-Review voter failures.
# Story S4b (epic d251).
# ---------------------------------------------------------------------------
# METRIC SOURCE: the review-bot container's stdout/stderr ships to journald (S2).
# When the voter fails to cast a vote (Gerrit 4xx/5xx, clone/diff failure, an
# unreachable LLM, or an expired token) it writes a structured `VOTER_ERROR` JSON
# line to stderr (rebar.review_bot.voter._voter_error). Rather than give the
# container AWS creds (the IMDS hop-limit constrains in-container metadata access),
# the HOST observability probe greps the review-bot container's journald for those
# `VOTER_ERROR` markers since the last run and publishes a custom CloudWatch metric
# — exactly the pattern S5 uses for replication_errors. This alarm watches THAT
# metric. If the probe is not yet publishing, the alarm sits in INSUFFICIENT_DATA
# (treated as not-breaching) rather than firing falsely.
#
# WHY THIS MATTERS: submit REQUIRES the LLM-Review vote (ADR-0013), so a voter that
# silently fails leaves changes unsubmittable (the fail-closed posture). The alarm
# surfaces a persistently broken voter to an operator instead of letting
# unsubmittable changes stack up unnoticed — there is deliberately NO break-glass to
# disable the submit requirement; the fix is to RESTORE the voter.
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = voter_errors
#   Dimensions = NONE — DIMENSIONLESS ON BOTH SIDES. The probe publishes with no
#                  dimensions and this alarm declares none. CloudWatch keys a metric
#                  by namespace+name+dimensions, so adding a dimension to ONLY one
#                  side makes the alarm silently stop matching. Change BOTH or neither.
#   Unit       = Count   (a per-period count of new VOTER_ERROR log lines)
#
# Reuses var.aws_region and data.aws_caller_identity.current (declared in iam.tf)
# for ARNs, matching the repo's existing patterns (see monitoring_s5.tf).
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "voter_errors" {
  alarm_name        = "rebar-gerrit-voter-errors"
  alarm_description = <<-EOT
    rebar review-bot LLM-Review voter failures detected in the receiver's journald
    log (VOTER_ERROR markers: Gerrit 4xx/5xx, clone/diff failure, LLM unavailable, or
    an expired bot token). Published as the custom metric rebar/host:voter_errors by
    the host observability probe. Because submit requires the LLM-Review vote
    (ADR-0013), a failing voter leaves changes unsubmittable — the fail-closed gate.
    Restore the voter (token / LLM / receiver); there is no break-glass to disable
    the submit requirement.
  EOT

  namespace   = "rebar/host"
  metric_name = "voter_errors"
  statistic   = "Sum"

  # 5-minute periods; alarm on a single period with any voter-error lines.
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # No voter errors published in a period is the healthy steady state and should NOT
  # alarm, so missing data is treated as not-breaching.
  treat_missing_data = "notBreaching"

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
