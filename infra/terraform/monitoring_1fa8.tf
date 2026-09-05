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
# alarm, INCLUDING its SNS wiring (like monitoring_ws7.tf), because a broken CI
# dispatcher silently blocks ALL submits once the gate is active — a gate-critical
# failure that must page an operator, not just sit on a dashboard.
#
# (This comment used to read "UNLIKE S4b it WIRES SNS". That was true when written, but it
# described a BUG in S4b rather than a deliberate difference: S4b is gate-critical for the
# same reason stated above — submit requires the LLM-Review vote — so it always should have
# notified. Ticket 9baf wired S4b and S5, and tests/unit/test_alarm_actions_terraform.py now
# enforces that every alarm notifies somebody, so no future alarm can be silently actionless.)
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
