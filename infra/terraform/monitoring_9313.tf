# ---------------------------------------------------------------------------
# monitoring_9313.tf — the observability probe's own liveness.
# Bug 9313-1fac-9f32-4b07.
# ---------------------------------------------------------------------------
# WHY THIS ALARM EXISTS AT ALL. Every other alarm on this box watches something the probe
# measures. Nothing watched the probe. On 2026-09-05 `rebar-observability.service` was being
# SIGTERM-ed on its 240s TimeoutStartSec on essentially every run under load — 55 timeout kills
# against 197 completed runs in 24h — and each kill deleted every metric published after the
# kill point. That was visible in `systemctl` and in NO metric, which is why it read as
# "sparse publishing" for a day.
#
# The failure is worse than a plain outage because it is INDISTINGUISHABLE from one on the
# metric side. Every alarm here is treat_missing_data = "breaching", so a truncated run and a
# genuinely breaching value both surface as ALARM. Of six alarms in ALARM at 17:09 UTC that
# day only two were real: docker_unaccounted_bytes was reading 1.87 GB against a 2 GiB
# threshold and BuildKit 10% against 85 while their alarms said otherwise. A third of the alarm
# surface was reporting the opposite of the truth.
#
# So the probe now publishes its own liveness and this watches it:
#   probe_ok         1, published ONLY after every section has run. Its ABSENCE means the run
#                    did not reach the end.
#   probe_truncated  published by the ExecStopPost hook, which systemd runs even after a
#                    timeout SIGTERM. 1 = the unit stopped for any reason other than success.
#
# Two alarms, because the two signals fail in different directions and either alone leaves a
# hole: probe_truncated catches a kill while the unit still runs at all, and the missing-data
# arm of probe_ok catches a run that starts and never reaches its end — which publishes no
# truncation signal either, since ExecStopPost reports how the unit STOPPED, not how far it got.
#
# Metric contract (what observability.sh PutMetricData's):
#   Namespace  = rebar/host
#   MetricName = probe_ok | probe_truncated | probe_elapsed_seconds
#   Dimensions = NONE — dimensionless on both sides, following mirror_out_of_sync
#                  (monitoring_ws7.tf): CloudWatch keys a metric by
#                  namespace+name+dimensions, so change BOTH sides or neither.
#   Unit       = Count (probe_ok, probe_truncated) / Seconds (probe_elapsed_seconds)
#
# Windows follow infra/runbooks/alarm-window-tuning.md Profile A: treat_missing_data = "breaching"
# with datapoints_to_alarm == evaluation_periods (I1) over a 1800 s window (I2). That runbook names
# these two metrics as the canonical dead-publisher and truncation signals once this change lands,
# so the heartbeat role its other alarms carry becomes a second line rather than the only one.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "observability_probe_truncated" {
  alarm_name        = "rebar-observability-probe-truncated"
  alarm_description = <<-EOT
    The host observability probe stopped for a reason other than success — normally
    SIGTERM on its TimeoutStartSec. A truncated run publishes only the metrics that
    came before the kill point, so treat EVERY other rebar/host alarm currently in
    ALARM as unproven until this clears: a gap and a breach are the same shape once
    treat_missing_data is "breaching". Published as rebar/host:probe_truncated by the
    ExecStopPost hook in install-observability.sh. Start with
    `journalctl -u rebar-observability.service` and the probe_elapsed_seconds metric,
    which says how close a completing run is coming to the 240s ceiling.
  EOT

  namespace   = "rebar/host"
  metric_name = "probe_truncated"
  statistic   = "Maximum" # 1/0 flag; alarm if any run in the window was truncated

  # I1 + I2 (infra/runbooks/alarm-window-tuning.md): datapoints_to_alarm == evaluation_periods,
  # over a 1800 s window. Authored before that runbook landed with M=2 of N=3, which the invariant
  # test added by ticket a9d1-c7f3-cfd9-44ff correctly rejected — under `breaching`, M < N lets
  # missing buckets OUT-VOTE a real datapoint, so an ordinary publish gap pages on its own and a
  # slow publisher holds the alarm in ALARM unclearably (measured: 10.5 hours on
  # rebar-docker-buildkit-cache-high). With M = N a single published datapoint anywhere in the
  # window makes the condition unsatisfiable and the alarm returns to OK immediately.
  #
  # 6 x 300 s = 30 min, not the 900 s I2 floor, matching Profile A. Two reasons, and the second is
  # the one that decides it. First, this probe's own inter-arrival CONTRACT is 5-9 minutes
  # (OnUnitActiveSec=5min measured from the last COMPLETED run, plus TimeoutStartSec), so a
  # 15-minute window is only two contractual arrivals. Second, and specifically: probe_ok is
  # emitted LAST in the script, so it gaps whenever a whole run is dropped — and the 25-minute
  # head-of-script gap measured at 20:47Z on 2026-09-05 is a SEPARATE, still-open defect
  # (a7bd-0cee-404f-4c06) that drops runs outright. A 15-minute window here would page on that
  # other bug. Revisit once a7bd is closed and the cadence re-measured; the measurement, not the
  # aspiration, should set it.
  #
  # The cost is stated rather than hidden: at M = N a SINGLE truncated run no longer pages. What
  # pages is 30 minutes of continuous truncation — which is the condition actually observed here
  # (55 kills against 197 runs in 24 h, clustered), not a lone blip. A one-off truncation is still
  # a published datapoint and still visible on the metric.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # DEAD-PUBLISHER, the same rule as every other rebar/host alarm (ticket bff5-9163-cddd-4158).
  # I first wrote "notBreaching" here, reasoning that ExecStopPost cannot run if the unit never
  # ran, so silence means "no runs at all" and the probe_ok alarm below already covers it. That
  # was wrong on the facts: ExecStopPost fires on EVERY stop, and a successful stop publishes
  # probe_truncated=0. A healthy period is therefore a published 0, never silence — exactly the
  # precondition the rule is stated against — so missing data here means the probe, its timer,
  # or the host is dead, and failing open would clear this alarm in the one situation where the
  # rest of the alarm surface is least trustworthy. Double-paging with the probe_ok alarm on a
  # dead host is the correct outcome, not a reason to fail open.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "9313-1fac-9f32-4b07"
  }
}

resource "aws_cloudwatch_metric_alarm" "observability_probe_not_completing" {
  alarm_name        = "rebar-observability-probe-not-completing"
  alarm_description = <<-EOT
    The host observability probe has not completed a full run recently. rebar/host:probe_ok
    is published once, after every section has had its turn, so its absence means runs are
    not reaching the end — or are not happening at all. While this is in ALARM every other
    rebar/host metric may be silently truncated and the alarms built on them cannot be
    trusted in either direction.
  EOT

  namespace   = "rebar/host"
  metric_name = "probe_ok"
  statistic   = "Maximum"

  # The complement of the alarm above: it fires on the ABSENCE of the completion heartbeat, so the
  # threshold arm is the degenerate one and treat_missing_data does the work. Same window and same
  # I1/I2 reasoning as that alarm — M = N = 6 over 1800 s, Profile A. Here M = N is what makes the
  # dead-man honest as well as clearable: one published probe_ok = 1 anywhere in the window proves
  # the probe reached the end of a run, and that single datapoint clears the alarm outright.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # DEAD-PUBLISHER, the same rule as mirror_out_of_sync: a healthy period is a published 1,
  # never silence, so missing data is the signal rather than an absence of one.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "9313-1fac-9f32-4b07"
  }
}
