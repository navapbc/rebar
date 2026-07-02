# ---------------------------------------------------------------------------
# monitoring_1fa8.tf — CloudWatch alarm for gerrit-to-platform CI-vote failures.
# Epic 1fa8 (the CI `Verified` second gate vote).
# ---------------------------------------------------------------------------
# METRIC SOURCE: the CI `Verified` vote rides two legs (ADR-0020/0022/0023):
#   (1) DISPATCH  Gerrit → GitHub: the `hooks` plugin execs the in-container g2p
#       console-scripts on patchset-created / `recheck`, which workflow_dispatch
#       gerrit-verify.yaml. g2p's stdout/stderr ships to the Gerrit container's
#       journald (compose-gerrit-1) — the same journald path S4b uses for the
#       review-bot. A dispatch failure (bad PAT, GitHub 4xx/5xx, g2p exception)
#       means NO Actions run fires, so NO Verified vote ever arrives.
#   (2) VOTE-BACK GitHub → Gerrit: the Actions run SSHes back to cast Verified.
#       That leg's success/failure is in the GitHub Actions run status (GitHub-side,
#       not on this host); the gate is fail-closed either way (no +1 ⇒ no submit).
# This alarm watches the HOST-OBSERVABLE leg (1): the host observability probe
# (infra/scripts/observability.sh §6) greps compose-gerrit-1's journald for g2p
# error markers since the last run and publishes a per-interval count — exactly the
# pattern S5/S4b use. If the probe is not yet publishing, the alarm sits in
# INSUFFICIENT_DATA (treated as not-breaching) rather than firing falsely.
#
# WHY THIS MATTERS: once the `Verified` submit requirement is ACTIVATED (story S6 /
# two-vote-gate-rollback.md), submit REQUIRES a Verified=MAX vote. A g2p dispatcher
# that silently fails leaves changes unsubmittable (the fail-closed posture) — the
# same failure mode the S4b voter alarm guards for the LLM-Review leg. This surfaces
# a broken CI dispatcher to an operator instead of letting unsubmittable changes
# stack up unnoticed.
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = g2p_dispatch_errors
#   Dimensions = NONE — DIMENSIONLESS ON BOTH SIDES. The probe publishes with no
#                  dimensions and this alarm declares none. CloudWatch keys a metric
#                  by namespace+name+dimensions, so adding a dimension to ONLY one
#                  side makes the alarm silently stop matching. Change BOTH or neither.
#   Unit       = Count   (a per-period count of new g2p error log lines)
#
# Reuses var.aws_region + data.aws_caller_identity.current (iam.tf) and the shared
# aws_sns_topic.alerts (monitoring.tf). Structurally this mirrors S4b's voter_errors
# alarm; UNLIKE S4b it WIRES SNS (like monitoring_ws7.tf), because a broken CI
# dispatcher silently blocks ALL submits once the gate is active — a gate-critical
# failure that must page an operator, not just sit on a dashboard.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "g2p_dispatch_errors" {
  alarm_name        = "rebar-gerrit-g2p-dispatch-errors"
  alarm_description = <<-EOT
    gerrit-to-platform CI-dispatch failures detected in the Gerrit container's
    journald (g2p error markers: a failed workflow_dispatch, GitHub 4xx/5xx, an
    expired PAT, or a g2p exception on patchset-created / recheck). Published as the
    custom metric rebar/host:g2p_dispatch_errors by the host observability probe
    (observability.sh §6). Once the Verified submit requirement is active, submit
    requires the CI Verified vote — a failing dispatcher leaves changes unsubmittable
    (the fail-closed gate). Fix the dispatcher (PAT / g2p / hooks plugin); the
    temporary back-out to single-vote gating is in
    infra/runbooks/two-vote-gate-rollback.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "g2p_dispatch_errors"
  statistic   = "Sum"

  # 5-minute periods; alarm on a single period with any g2p error lines.
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # No dispatch errors published in a period is the healthy steady state and should
  # NOT alarm, so missing data is treated as not-breaching.
  treat_missing_data = "notBreaching"

  # WIRE SNS (unlike S5/S4b): a broken CI dispatcher blocks all submits once the gate
  # is active, so page the operator (same choice as monitoring_ws7.tf).
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "1fa8-S6"
  }
}

# Discoverability anchor: the alarm's metric is account/region-scoped under this
# identity (mirrors the locals in monitoring_s5.tf / monitoring_s4b.tf).
locals {
  g2p_alarm_region     = var.aws_region
  g2p_alarm_account_id = data.aws_caller_identity.current.account_id
}
