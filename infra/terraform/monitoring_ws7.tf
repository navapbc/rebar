# ---------------------------------------------------------------------------
# monitoring_ws7.tf — CloudWatch alarm for Gerrit -> GitHub mirror out-of-sync.
# Ticket a774 (epic b744), post-WS7 cutover.
# ---------------------------------------------------------------------------
# WHY THIS IS DISTINCT FROM THE S5 replication_errors ALARM: S5 counts FAILURE
# LINES in the replication_log. But a replication that silently stops firing (or a
# push that is never attempted) logs no failure, so S5 stays green while GitHub
# `main` quietly falls behind Gerrit `main`. This alarm watches the ACTUAL end
# state: are the two `main` SHAs equal? The host probe (observability.sh section 5)
# compares the anonymous Gerrit REST revision against a public `git ls-remote` of
# GitHub and publishes mirror_out_of_sync = 1 (diverged) / 0 (in sync).
#
# Custom metric contract (what the host probe PutMetricData's):
#   Namespace  = rebar/host
#   MetricName = mirror_out_of_sync
#   Dimensions = NONE — DIMENSIONLESS ON BOTH SIDES (same rule as S5: CloudWatch keys
#                  a metric by namespace+name+dimensions; change BOTH sides or neither).
#   Unit       = Count   (1 = diverged, 0 = in sync)
#
# Reuses var.aws_region + data.aws_caller_identity.current (iam.tf) and the shared
# aws_sns_topic.alerts (monitoring.tf). Unlike the S5/S4b alarms, this one WIRES SNS
# (a774 requires an actual alert): a sustained mirror divergence pages the operator.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "mirror_out_of_sync" {
  alarm_name        = "rebar-gerrit-mirror-out-of-sync"
  alarm_description = <<-EOT
    GitHub `main` has diverged from Gerrit `main` for a sustained window — Gerrit
    is the source of truth and replication should keep GitHub in lockstep, so a
    persistent divergence means replication is stuck/failing and the mirror is
    stale. Published as the custom metric rebar/host:mirror_out_of_sync (1=diverged)
    by the host observability probe (observability.sh section 5). See
    infra/runbooks/github-mirror-lock.md for the replication-failure rollback trigger.
  EOT

  namespace   = "rebar/host"
  metric_name = "mirror_out_of_sync"
  statistic   = "Maximum" # the flag is 1/0; alarm if it is 1 across the window

  # PROFILE A (dead-man), 8-of-8 over 40 minutes — infra/runbooks/alarm-window-tuning.md.
  #
  # The 2-of-3 shape this replaces PAGED ON HEALTHY OPERATION and is the defect ticket
  # a9d1-c7f3-cfd9-44ff was opened for. With treat_missing_data = "breaching" an empty period
  # IS a breaching datapoint, so 2-of-3 fires on one divergence sample plus one ordinary
  # scheduling gap — the reconstructed firing window was literally `1, MISSING, 1`. Its
  # justification ("~22 of 24 periods present is the observed norm") was also false: 6 of 47
  # five-minute buckets over four hours were empty, so 87%, not 92%.
  #
  # datapoints_to_alarm == evaluation_periods is the fix, not the wider window. It means a
  # page requires EVERY reading in the 40-minute window to be 1, so:
  #   - the `1, MISSING, 1` firing cannot recur: the probe publishes 0 on the runs between two
  #     independent sub-minute catches of fresh submits, and any single 0 clears the window;
  #   - the alarm can always return to OK on ONE healthy datapoint, however slow the publisher
  #     is. Under 2-of-3 a publisher slower than the window re-armed the alarm on every
  #     evaluation forever (measured on rebar-docker-buildkit-cache-high: 10.5 hours stuck).
  #
  # 40 minutes is the detection bound for a genuinely stuck mirror. Replication lag is normally
  # ~15s and was measured at up to 2m44s, so 40 minutes of CONTINUOUS divergence is unambiguous,
  # and the harm this guards — CI testing a different tree than Gerrit's main — accrues over
  # hours, not minutes.
  period              = 300
  evaluation_periods  = 8
  datapoints_to_alarm = 8
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # DEAD-PUBLISHER, not "quiet when healthy" (ticket bff5-9163-cddd-4158). The probe
  # publishes mirror_out_of_sync on EVERY 5-minute run — 0 when the SHAs match, and (since
  # the same ticket) 1 when the comparison itself could not be made. A healthy period is a
  # published 0, never silence, so missing data means the probe, its timer, or the host is
  # dead — and a dead probe cannot notice that GitHub `main` has stopped tracking Gerrit.
  # The earlier "the probe fails safe" rationale had it backwards: it failed OPEN.
  #
  # §5 is the LAST section of observability.sh, which makes this alarm the tail-of-script
  # liveness sentinel: a run truncated by the unit's 240s TimeoutStartSec never reaches §5, and
  # 40 minutes of that silence pages here.
  #
  # THAT DUTY IS LOAD-BEARING FOR NINE OTHER ALARMS. Ticket a9d1 moved the error counters to
  # treat_missing_data = "notBreaching", which makes "the publisher is dead" and "there were no
  # errors" the same observation on those metrics. Their dead-man is THIS alarm, and it works
  # only because mirror_out_of_sync is published at line 1511 while all nine publish at
  # 1193-1463: publication is sequential, so this metric's silence is a superset of theirs. The
  # early heartbeats do NOT cover them — measured, mcp_healthy and gate_scratch_mounted were
  # present in 54 buckets where g2p_dispatch_errors was absent, because the truncation lands
  # between them; this sentinel's residual is at most 3 buckets of 41, and 0 for the counter
  # published immediately before it.
  #
  # So DO NOT move this publish earlier in observability.sh, and do not add a counter after it.
  # tests/unit/test_alarm_window_tuning.py fails the build on either — without that guard the
  # change would remove the dead-man from nine alarms with no other symptom. Residual and the
  # follow-up that retires this arrangement (9313's purpose-built probe_ok / probe_truncated)
  # are in infra/runbooks/alarm-window-tuning.md.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "WS7-a774"
  }
}
