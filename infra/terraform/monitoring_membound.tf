# ---------------------------------------------------------------------------
# monitoring_membound.tf — host MEMORY alarms. Story 48f0-f7ff-c8df-43ac
# (cryptozoic-unlit-ladybug), under epic 0480-df66-b3df-4aee.
# ---------------------------------------------------------------------------
# WHY THESE DID NOT EXIST. `rebar/host:mem_available_percent` has been published by
# infra/scripts/observability.sh since 2026-09-03T17:00Z and was referenced by ZERO
# alarms: of the 30 alarms in this tree, none watched host memory. The 2026-09-05
# wedge — the host stopped serving entirely and only a stop/start recovered it — ran
# up over seven hours at 11-13% available memory with nothing watching.
#
# MEASURED BASIS (CloudWatch, i-00880b2c7f13527c5, the metric's ENTIRE published
# history: 48 hourly buckets, 2026-09-03T17:00Z - 2026-09-06T01:00Z).
#
#   Hourly minima below 20% available — eight hours, and they separate cleanly:
#     2026-09-04 20:00 PDT   16%   no incident
#     2026-09-05 03:00 PDT   13%  ┐
#     2026-09-05 04:00 PDT   11%  │
#     2026-09-05 05:00 PDT   12%  │ the wedge run-up
#     2026-09-05 06:00 PDT   13%  │
#     2026-09-05 08:00 PDT   11%  │
#     2026-09-05 09:00 PDT   11%  │
#     2026-09-05 10:00 PDT   11%  ┘
#
#   Per-container `container_memory_rss_bytes`, 48h Maximum: gerrit 1.19 GB,
#   mcp 4.64 GB, review-bot 2.43 GB, opcert 0.05 GB. Sum 8.31 GB against 7.8 GiB
#   (8.37 GB) of RAM — the sum of observed peaks is 99% of physical memory.
#
# THE PUBLISHER IS THE HARD PART, AND IT IS WHY THESE TWO ALARMS ARE SHAPED
# DIFFERENTLY FROM EVERY OTHER rebar/host ALARM.
#
# Measured on the SAME probe runs, 2026-09-05 12:00-16:00 PDT:
#     mcp_healthy            (observability.sh head, ~line 131)   7-12 samples/hour
#     mem_available_percent  (observability.sh tail, ~line 1018)  ZERO, five hours
# and on a healthy idle box over the following three hours:
#     mcp_healthy            29/36 five-minute buckets  (81%)
#     mem_available_percent   5/36                      (14%)
#
# The probe did NOT die during those five hours — head-of-script metrics published
# throughout. It is SIGTERM-ed on its 240 s TimeoutStartSec before reaching the
# memory read (root cause: bug ignitable-fuchsia-kawala / 9313-1fac-9f32-4b07; the
# fix is change 2684). So the memory signal is ABSENT 73-86% of the time, and — the
# part that matters — its absence is ANTI-CORRELATED with the condition it measures:
# it disappears hardest exactly when the host is under the pressure it would report.
#
# A `treat_missing_data = "breaching"` threshold alarm on a metric that absent would
# be a missing-data pager, not a memory detector, and the honest response to its
# noise would be to widen its window — the accommodation spiral epic 0480 exists to
# stop. The two alarms below split that problem instead of averaging it:
#
#   * the THRESHOLD alarm fails OPEN on absence (notBreaching), so it only ever
#     speaks about readings it actually got, and carries the opt-out marker the
#     host-alarm guard requires, naming the dead-man below as absence's owner;
#   * the DEAD-MAN alarm owns absence exclusively, and is built on the head-vs-tail
#     discriminator above rather than on bare absence, with its window sized from the
#     measured run-length distribution rather than from a round number.
#
# Neither window is widened to buy quiet. Both record the value to re-tune to once
# change 2684 restores tail coverage.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "host_memory_low" {
  alarm_name = "rebar-host-memory-low"

  alarm_description = <<-EOT
    Host available memory fell below 15%. On 2026-09-05 the box sustained 11-13% for
    seven hours and then wedged completely, recovering only on a stop/start; every
    other candidate (EBS burst, CPU throttling, hypervisor, disk) was ruled out with
    data. TRIAGE: `free -m` and `docker stats --no-stream` for which container grew;
    the sum of observed container peaks is 99% of the 7.8 GiB of RAM, so any excursion
    is over the line. Gates are capped at 2 concurrent and refuse below 1024 MiB
    available, so a breach here means a container grew rather than gates piling up.
    Recovery: infra/runbooks/gerrit-host-wedged-ssm-lost.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "mem_available_percent"

  # Minimum, not Average: a five-minute bucket containing one reading at 11% and one at
  # 40% is a bucket in which the host was at 11%. Averaging hides exactly the excursion.
  statistic = "Minimum"

  # 15, from the measurement above and not from a round number. It is the only integer
  # that separates the wedge band (11-13%, seven hours) from every non-incident hourly
  # minimum in the metric's whole history (lowest 16%). THE MARGIN IS ONE POINT, which
  # is thin and is stated rather than hidden: if a non-incident hour ever dips below
  # 15%, this threshold is wrong and should move down, not have its window widened.
  threshold           = 15
  comparison_operator = "LessThanThreshold"

  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 1

  # rebar:allow-missing-data-notbreaching: the mem_signal_absent_while_probe_alive dead-man below
  # owns the absent case exclusively. Measured coverage for THIS metric is 14-27% (see the
  # header) because the probe truncates before reaching it, so `breaching` here would page
  # on absence roughly three times in four and say nothing about memory. Failing open is
  # what keeps this alarm honest about the readings it does get.
  treat_missing_data = "notBreaching"

  # 1-of-6 IS CONTINGENT, AND THE DEPENDENCY IS THE POINT. At 14% coverage a 30-minute
  # window holds ~0.9 expected readings, so requiring more than one datapoint would make
  # this alarm unfireable in practice — an alarm that cannot fire is as useless as one that
  # always fires (infra/runbooks/alarm-window-tuning.md, invariant I3). The detection value
  # of this alarm is therefore CONTINGENT ON CHANGE 2684 landing and restoring tail
  # coverage; until then it catches a sustained squeeze late rather than a transient dip
  # early. RE-TUNE TARGET, to apply once mem_available_percent coverage is >= 80% over a
  # 24-hour sample (measure with `get-metric-statistics --period 300 --statistics
  # SampleCount`): datapoints_to_alarm = 3, which still satisfies I3 at this window
  # (3 x 600 s = 1800 s = period x evaluation_periods) and stops a single dip paging.
  # I3 holds at the current 1-of-6 as well: 1 x 600 s <= 1800 s.

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "48f0-f7ff-c8df-43ac"
  }
}

resource "aws_cloudwatch_metric_alarm" "mem_signal_absent_while_probe_alive" {
  alarm_name = "rebar-mem-signal-absent-while-probe-alive"

  alarm_description = <<-EOT
    The host memory reading has been missing for three hours while the observability probe
    was still running, so the box is operating memory-blind. On 2026-09-05
    mem_available_percent went silent for FIVE consecutive hours immediately before the host
    wedged and nothing observed it. TRIAGE: the probe is alive (head-of-script metrics are
    publishing), so it is being SIGTERM-ed on its 240s budget before the memory read --
    `systemctl status rebar-observability.service`, `journalctl -u rebar-observability` for
    the kill, and check what is stalling ahead of it (bug ignitable-fuchsia-kawala). A probe
    that has died ENTIRELY does not reach this alarm; that case belongs to
    rebar-mcp-serving-path-down.
  EOT

  # BUILT ON THE TRUNCATION DISCRIMINATOR, NOT ON BARE ABSENCE. A plain `breaching` window on
  # mem_available_percent cannot tell "the probe was cut short" from "the host is gone", and
  # the measurement in the header shows the first case is the common one -- so such an alarm
  # would page overwhelmingly for the wrong reason. This expression asks the question that
  # actually distinguishes them:
  #
  #   e1 = FILL(m1, -1)   memory reading, or -1 where the period has none
  #   e2 = FILL(m2, 0)    how many times the probe's HEAD published in the period
  #   e3 = IF(e1 < 0 AND e2 > 0, 1, 0)   1 == "the probe ran, and memory is still missing"
  #
  # Probe dead ENTIRELY: e2 is 0, so e3 is 0 and this alarm stays silent -- deliberately,
  # because rebar-mcp-serving-path-down already owns that case with its own dead-man.
  #
  # VERIFIED READ-ONLY against the real series with `aws cloudwatch get-metric-data` (the
  # same expression engine an alarm evaluates), because FILL over a metric with NO datapoints
  # in the window was the obvious way for this to fail silently. It does not: across the
  # incident window 2026-09-05 19:00-23:00Z the expression returns 48 datapoints, every one
  # of them 1, even though mem_available_percent published nothing at all -- the sibling
  # metric supplies the time grid FILL needs.
  metric_query {
    id          = "e3"
    expression  = "IF(e1 < 0 AND e2 > 0, 1, 0)"
    label       = "memory reading missing while the probe is alive"
    return_data = true
  }

  metric_query {
    id          = "e1"
    expression  = "FILL(m1, -1)"
    label       = "mem_available_percent, -1 where absent"
    return_data = false
  }

  metric_query {
    id          = "e2"
    expression  = "FILL(m2, 0)"
    label       = "probe head publish count, 0 where absent"
    return_data = false
  }

  metric_query {
    id          = "m1"
    return_data = false
    metric {
      namespace   = "rebar/host"
      metric_name = "mem_available_percent"
      period      = 300
      stat        = "Minimum"
    }
  }

  metric_query {
    id          = "m2"
    return_data = false
    # mcp_healthy publishes near the HEAD of observability.sh and is the least
    # truncation-prone signal the probe has (measured 81% five-minute coverage against this
    # metric's 14% over the same runs), which is exactly what makes it usable as "the probe
    # ran". SampleCount, not the value: this asks WHETHER it published, never whether mcp
    # was healthy -- an unhealthy mcp publishing 0 still proves the probe reached line 131.
    metric {
      namespace   = "rebar/host"
      metric_name = "mcp_healthy"
      period      = 300
      stat        = "SampleCount"
    }
  }

  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # 36 OF 36 IS MEASURED, NOT ROUNDED. The expression above is 1 during ordinary truncation
  # too, so the window is what separates "the tail is being cut short, as it constantly is"
  # from "we have been blind for hours". Both runs were measured on the real series:
  #
  #   ordinary healthy-but-truncated operation, 6h to 2026-09-06T00:56Z
  #       23 buckets 0, 49 buckets 1, LONGEST CONSECUTIVE RUN OF 1s = 15
  #   the incident, 2026-09-05 19:00-23:00Z
  #       48 consecutive 1s (and the silence ran ~5h in total)
  #
  # 36 sits between them: 2.4x the longest run ordinary truncation has produced, and inside
  # the incident's run with room to spare, so this would have fired ~3h into that silence,
  # before the wedge. It is NOT widened beyond that to buy quiet -- anything wider trades
  # away the detection the alarm exists for.
  #
  # THE FALSIFIABLE CLAIM: ordinary truncation never sustains 36 consecutive periods without
  # a memory reading. Disproof is a false page here with the probe healthy, which would mean
  # truncation has worsened past what was measured -- in which case the fix is change 2684,
  # not a wider window.
  #
  # RE-TUNE TARGET once 2684 restores tail coverage to >= 80%: evaluation_periods = 6, the
  # standard 30-minute Profile A dead-man shape (infra/runbooks/alarm-window-tuning.md).
  # NO top-level `period` here: AWS rejects it as "conflicts with metric_query" (terraform
  # validate, 2026-09-06). The 300 s cadence is declared inside the two metric blocks above,
  # which is also where the window guards in tests/unit/test_alarm_window_tuning.py read it
  # from, so period x evaluation_periods still evaluates to the 10800 s window described here.
  evaluation_periods  = 36
  datapoints_to_alarm = 36

  # breaching, which makes the alarm's condition a SUPERSET of the expression alone: 36
  # consecutive periods that are each either "memory absent while the probe ran" (the
  # expression at 1) or "neither metric published at all" (missing). The second case is a dead
  # probe, which rebar-mcp-serving-path-down also owns -- so this can double-page one outage,
  # and that is the accepted cost of not letting a dead publisher read as health here.
  #
  # It is NOT a free choice. `notBreaching` was tried first and is forbidden by invariant I3 in
  # infra/runbooks/alarm-window-tuning.md: a non-breaching alarm must budget >= 600 s of
  # publisher time per REQUIRED datapoint, and 36 required datapoints inside a 10800 s window
  # budgets only 300 s each. Satisfying I3 while staying non-breaching would mean dropping to
  # <= 18 of 36, and that is not the same condition at all -- 18-of-36 counts ANY 18 buckets in
  # the window rather than a consecutive run, and the measurement above (49 of 72 buckets at 1
  # during ordinary truncation) says such an alarm would fire more or less continuously today.
  # So: breaching, with M == N, which satisfies I1 and I2 and keeps the run-length semantics
  # the window was measured for.
  #
  # NOTE FOR ANYONE COPYING THIS: a metric-math alarm is NOT exempt from the rebar/host guards
  # in test_alarm_actions_terraform.py. Those read `namespace` with a MULTILINE regex, which
  # matches the value nested inside a metric_query block just as readily as a top-level one.
  # monitoring_eb6e.tf's Bedrock alarm is excluded because its namespace VALUE is AWS/Bedrock,
  # not because its declaration is nested. This alarm names rebar/host, so every host-alarm
  # invariant applies to it, and it satisfies them rather than opting out.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "48f0-f7ff-c8df-43ac"
  }
}
