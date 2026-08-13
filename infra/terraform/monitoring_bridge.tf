# ---------------------------------------------------------------------------
# monitoring_bridge.tf — Reconcile Bridge failure visibility (ticket 58e0-ca58-c5ab-4322)
# ---------------------------------------------------------------------------
# THE GAP THIS CLOSES. On 2026-08-12 the JIRA_API_TOKEN expired and the Reconcile Bridge failed
# for hours with ZERO CloudWatch signal: voter_errors stayed 0 across the whole window and the
# rebar/host namespace had no bridge counter at all (19 metric names, none bridge-related). That
# is structural, not an oversight — every rebar/host metric is published by the host probe
# (infra/scripts/observability.sh) running on the Gerrit box, and the bridge runs on GitHub
# Actions, where that probe has no reach. The workflow now publishes the metric itself.
#
# Custom metric contract (what .github/workflows/reconcile-bridge.yml PutMetricData's):
#   Namespace  = rebar/host
#   MetricName = bridge_run_failures
#   Dimensions = NONE — dimensionless on BOTH sides (monitoring_s4b.tf: a dimension on only one
#                side silently unmatches and the alarm goes permanently INSUFFICIENT_DATA).
#   Unit       = Count — 1 per FAILED run, 0 per healthy run. The 0 is deliberate: it keeps the
#                metric continuously present, so "healthy" is distinguishable from "the publisher
#                is broken" and the alarm rests in OK rather than INSUFFICIENT_DATA.
#
# CADENCE DIFFERS FROM THE rebar-autodeploy-* ALARMS, deliberately. Those use 900s / threshold 0,
# latching on a single event, because a review interrupt cannot recur faster than one per episode
# and the signal-unavailable case must never hide. The bridge is the opposite shape: a lone failed
# pass is routinely a transient Jira/API/network blip that the next pass heals by itself, while the
# failure mode worth paging for (an expired token, a revoked credential, a malformed Variable) is
# SUSTAINED and fails every subsequent pass.
#
# The ORIGINAL tuning expressed that as "2 failures inside one hour" (period 3600, threshold 1,
# GreaterThanThreshold), which only worked while the bridge ran sub-hourly. The bridge now runs
# HOURLY (ticket 4557-1f33-7a3f-47c7), so at most ONE pass falls inside an hourly bucket and that
# Sum could never exceed 1 — the alarm would have become structurally unfirable, silently, which
# is the exact failure this metric exists to prevent (the 2026-08-12 token expiry produced hours
# of outage and zero CloudWatch signal).
#
# So the same intent is re-expressed in a shape an hourly metric can carry: period 3600 with
# threshold 0 over TWO CONSECUTIVE evaluation periods. A failed pass publishes 1 (breaching), a
# healthy pass publishes 0 (not breaching, which breaks the streak), so a single blip still stays
# quiet and only a sustained outage pages. Detection latency moves from roughly 40 minutes to
# roughly 2 hours — the price of the hourly cadence, not of this tuning.
#
# treat_missing_data = "notBreaching" — matching the autodeploy alarms. Note what that means
# here: a bridge that stops running ENTIRELY publishes nothing and will NOT alarm through this
# path. That case is already owned by the Reconciler Heartbeat Canary (which files a bug ticket
# on staleness); this alarm is for runs that execute and fail, which the canary cannot see
# quickly. The two are complements, not substitutes.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "bridge_run_failures" {
  alarm_name        = "rebar-bridge-run-failures"
  alarm_description = <<-EOT
    The Reconcile Bridge (GitHub Actions, .github/workflows/reconcile-bridge.yml) failed in TWO
    CONSECUTIVE hours, published as rebar/host:bridge_run_failures (1 per failed run, 0 per
    healthy run). MEANING: the Jira<->rebar reconciler is not converging, and ticket state drifts
    silently for as long as it stays down. The archetype is the 2026-08-12 JIRA_API_TOKEN expiry:
    hours of failure with no CloudWatch signal at all before this metric existed. REMEDIATION:
    open the newest Reconcile Bridge workflow run and read its failing step, then check
    JIRA_API_TOKEN validity (expiry is the most common cause) plus the JIRA_URL / JIRA_USER
    repo Variables. A SINGLE failed pass deliberately does not alarm — that is usually a
    transient API blip the next hourly pass heals; a healthy pass publishes 0 and breaks the
    streak. If the workflow has stopped running
    altogether this alarm stays OK by design; the Reconciler Heartbeat Canary owns that case.
  EOT

  namespace   = "rebar/host"
  metric_name = "bridge_run_failures"
  statistic   = "Sum"

  # See the cadence rationale above: a failed pass in each of two CONSECUTIVE hours. At the
  # hourly bridge cadence a "2 failures within one hour" threshold could never be reached.
  period              = 3600
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "58e0"
  }
}
