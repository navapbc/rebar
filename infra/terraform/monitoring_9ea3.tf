# Watch the current MCP serving path, separate from deploy-abort alarms.
# Every host-probe tick GETs the public /mcp URL through TLS/nginx. Only the
# application's unauthenticated 401 counts healthy (1), all other responses
# count 0. Match dimensionless rebar/host:mcp_healthy on both publisher and
# alarm, and notify SNS when the public path stops answering.

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
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # DEAD-PUBLISHER, not "quiet when healthy" (ticket bff5-9163-cddd-4158). §1b publishes
  # mcp_healthy on EVERY 5-minute run — 1 healthy, 0 unhealthy, 0 when the probe itself
  # could not complete — so a healthy period is a published 1, never silence. Missing data
  # therefore means the probe, its timer, or the host is dead, and a dead probe cannot
  # notice that /mcp has stopped serving.
  treat_missing_data = "breaching"

  # PROFILE A: 6-of-6 over 30 minutes. One healthy 1 clears ordinary empty
  # buckets. The probe can leave a 25-minute head-of-script gap, so a 15-minute
  # breaching-missing window falsely paged. See alarm-window-tuning.md.

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "9ea3-7d07-ea55-4496"
  }
}
