# ---------------------------------------------------------------------------
# monitoring_9ea3.tf — CloudWatch alarm for the rebar MCP SERVING PATH being down.
# Bug 9ea3-7d07-ea55-4496 (witted-invisible-roan).
# ---------------------------------------------------------------------------
# THE OUTAGE THIS EXISTS FOR: on 2026-09-02 the mcp container was OOM-killed ~3 minutes
# into a plan-review gate run (docker inspect: OOMKilled=true, Exit=137). nginx stayed
# healthy, but `upstream rebar_mcp` is a SINGLE materialized `server 127.0.0.1:<port>;`
# line with no failover, so the edge kept proxying to a dead backend and /mcp returned
# 502 for ~12 HOURS. A human reported it, because nothing in the system could observe it:
# gerrit_healthy and reviewbot_healthy were both 1 (they probe different services), the
# deploy declares success at the cutover and never re-validates the live upstream
# afterwards, and the only mcp metrics that existed — mcp_retire_cap and mcp_mem_abort
# (monitoring_foxterrier.tf) — are DEPLOY-PATH markers that read 0 throughout, because a
# kernel OOM-kill of an already-deployed container is not a deploy event.
#
# WHY IT IS DISTINCT FROM THE foxterrier ALARMS: those watch whether a RELEASE can
# converge, and both are fail-safe conditions (the current backend stays live). This one
# watches whether the CURRENT backend is still serving. They are complementary and neither
# implies the other — the outage had this one breaching and both of those at zero.
#
# Custom metric contract (what the host probe PutMetricData's; monitoring_s4b.tf rationale):
#   Namespace  = rebar/host
#   MetricName = mcp_healthy
#   Dimensions = NONE — dimensionless on BOTH sides (CloudWatch keys a metric by
#                namespace+name+dimensions; a dimension on one side only silently
#                unmatches and the alarm sits in INSUFFICIENT_DATA forever).
#   Unit       = Count  (1 = the serving path answered as the mcp app, 0 = it did not)
#
# THE PROBE (infra/scripts/observability.sh §1b) issues an unauthenticated GET to
# https://<domain>/mcp — the exact URL a client uses, through TLS, nginx, and the
# materialized upstream include — and publishes 1 IFF the response is 401, the auth
# challenge that only the mcp application itself can produce (nginx synthesises no 401
# for that location). 502/503/504, 000, and 404 all publish 0. It publishes on EVERY
# 5-minute tick including the failure paths, so this is a heartbeat, not an event.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "mcp_serving_path_down" {
  alarm_name = "rebar-mcp-serving-path-down"
  # NOTE: AWS caps alarm_description at 1024 characters and rejects longer ones at
  # apply time, so keep this terse and put procedure in the runbook. See the guard
  # test_alarm_descriptions_fit_the_aws_limit in tests/unit/test_alarm_actions_terraform.py.
  alarm_description = <<-EOT
    The rebar MCP serving path is not answering as the MCP application. The host probe
    (observability.sh section 1b) GETs /mcp through nginx and publishes
    rebar/host:mcp_healthy = 1 only when the response is the app's own 401 auth
    challenge; it published 0, so the request did not reach a live mcp backend.
    LIKELY CAUSE: the mcp container died (bug 9ea3 was an OOM-kill, Exit=137) while
    nginx's single-server `upstream rebar_mcp` kept pointing at it, so /mcp 502s while
    nginx itself stays healthy. TRIAGE: `docker ps -a | grep rebar-mcp` and
    `docker inspect` the newest for OOMKilled/ExitCode; `cat /etc/nginx/mcp-upstream.conf`
    for the bound port. Full recovery procedure: infra/runbooks/gerrit-host-wedged-ssm-lost.md.
    Gate runs driven through MCP fail while this is breaching.
  EOT

  namespace   = "rebar/host"
  metric_name = "mcp_healthy"

  # Minimum, not Average: the flag is 1/0 and a single unhealthy probe inside a period must
  # pull the period's datapoint below the threshold rather than being averaged away.
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # DEAD-PUBLISHER, not "quiet when healthy" (ticket bff5-9163-cddd-4158). §1b publishes
  # mcp_healthy on EVERY 5-minute run — 1 healthy, 0 unhealthy, 0 when the probe itself
  # could not complete — so a healthy period is a published 1, never silence. Missing data
  # therefore means the probe, its timer, or the host is dead, and a dead probe cannot
  # notice that /mcp has stopped serving.
  treat_missing_data = "breaching"

  # 300 / 3 / 2 is root_disk_pressure's shape, required by the guard in
  # tests/unit/test_alarm_actions_terraform.py: with breaching missing data, a live sample
  # showed ~22 of 24 five-minute periods present, so an isolated jittered interval is NORMAL
  # and a 1-of-N latch would page on it. Two breaching datapoints inside 15 minutes is well
  # under the 12-hour detection gap this alarm closes.

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "9ea3-7d07-ea55-4496"
  }
}
