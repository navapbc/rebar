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
    SUSTAINED rebar review-bot LLM-Review voter failure detected in the receiver's
    journald log (VOTER_ERROR markers: Gerrit 4xx/5xx, clone/diff failure, LLM
    unavailable, or an expired bot token). Published as the custom metric
    rebar/host:voter_errors by the host observability probe. Because submit requires
    the LLM-Review vote (ADR-0013), a failing voter leaves changes unsubmittable —
    the fail-closed gate. Restore the voter (token / LLM / receiver); there is no
    break-glass to disable the submit requirement. Calibrated to sustained-failure
    detection (ticket ea5d-4932-8554-4544): 3 of 5 five-minute periods with any
    markers — an isolated self-healing transient (timeout->backfill retry, clone
    stall, retry-budget fail-closed -1) never pages, even straddling a period
    boundary; a stuck gate emits markers on every retry (~5-min reconciler cadence)
    and pages in ~15 minutes.
  EOT

  namespace   = "rebar/host"
  metric_name = "voter_errors"
  statistic   = "Sum"

  # Sustained-failure shape (ticket ea5d-4932-8554-4544, rewindowed by a9d1-c7f3-cfd9-44ff):
  # 3 of 6 five-minute periods with any voter-error lines. The single-period shape this replaces paged 5 times
  # in 48h, every one a transient self-healed by a designed recovery path; the
  # operator ruling made PERSISTENCE the discriminator, not magnitude. Sizing:
  #   - datapoints_to_alarm = 3 so an isolated transient — at most 1 breaching
  #     period, or 2 when it straddles a period boundary — never pages (the 2-of-2
  #     house shape in monitoring_ws7.tf would page on that straddle).
  #   - A stuck gate (expired token / LLM outage) emits a marker on every review
  #     attempt — the webhook queue and the backfill reconciler retry continuously
  #     (RECONCILE_INTERVAL_SECONDS = 300) — so a continuous stream reaches its 3rd
  #     breaching period at ~15 min, the ruling's detection bound.
  #   - evaluation_periods = 6 (N-of-M, not N-of-N consecutive) because the marker
  #     stream is not phase-aligned with CloudWatch period boundaries: a sustained
  #     outage can land two markers in one period and none in the next, and a
  #     consecutive-N streak would reset on that gap. This was 5; a9d1 widened it to
  #     6 so that 3 REAL datapoints fit inside the window once missing periods stopped
  #     counting toward the total (worst-case page at 30 min, nominal 15).
  # PROFILE B (error counter), 3-of-6 over 30 minutes — infra/runbooks/alarm-window-tuning.md.
  #
  # 3-of-5 was the shape ticket a9d1-c7f3-cfd9-44ff was opened for: with treat_missing_data =
  # "breaching" (below) an empty period IS a breaching datapoint, so three ordinary scheduling
  # gaps satisfied datapoints_to_alarm on their own, with the counter reading 0 throughout.
  # Observed 2026-09-05: four counters in ALARM at once, each StateReason reading "1 datapoint was
  # received for 3 periods and 2 missing datapoints were treated as [Breaching]".
  #
  # datapoints_to_alarm is UNCHANGED at 3, deliberately. This alarm must keep catching an
  # INTERMITTENT error stream — one that is not phase-aligned with CloudWatch period boundaries
  # and lands markers in one period and none in the next — which an N-of-N streak would reset on.
  # So the M < N sensitivity stays and the missing-data rule changes instead (below). Only
  # evaluation_periods widens, to 6, so that 3 REAL datapoints are reachable inside the window
  # at the publisher's contractual 5-9 minute inter-arrival (install-observability.sh:
  # OnUnitActiveSec=5min measured from the last COMPLETED run, plus TimeoutStartSec=240). An
  # alarm that cannot fire is as useless as one that always fires.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 3
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

  # Notify the shared alerts topic on BOTH edges (ticket 9baf). This alarm previously declared
  # neither, so it transitioned OK -> ALARM and told nobody — the "silent-alarm gap" named in
  # monitoring.tf. That silence contradicted this file's own contract above, which claims the
  # alarm "surfaces a persistently broken voter to an operator". Because submit REQUIRES the
  # LLM-Review vote (ADR-0013), a failing voter is gate-critical and must page, not just sit on
  # a dashboard. ok_actions is wired too so a recovery is announced and the alarm does not read
  # as permanently firing.
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
