# The host probe counts two distinct MCP deploy backoffs in rebar/host:
# mcp_retire_cap means managed ports remain occupied by draining containers.
# mcp_mem_abort means the box lacks memory for blue-green overlap. Both leave
# the old backend serving, unlike the public-path mcp_healthy alarm.
# Publish dimensionless zero on healthy ticks. Each alarm uses a 900-second
# 2-of-4 window, notBreaching missing data, and shared SNS notification.

resource "aws_cloudwatch_metric_alarm" "mcp_retire_cap" {
  alarm_name        = "rebar-autodeploy-mcp-retire-cap"
  alarm_description = <<-EOT
    The mcp blue-green autodeploy target hit its managed-container / blue-green port-pool cap:
    both blue and green ports were held by un-reaped mcp containers still draining, so a new
    release backed off rather than force-killing a live container (AUTODEPLOY_MCP_RETIRE_CAP in
    the rebar-autodeploy.service journal; published as rebar/host:mcp_retire_cap by the host
    probe, observability.sh 4f). MEANING: the deploy is fail-safe (the current mcp backend stays
    live), but a sustained signal means releases are NOT draining — a stuck or chronically-busy
    mcp container is pinning the {8091, blue, green} port pool so new releases cannot deploy.
    REMEDIATION: `journalctl -u rebar-autodeploy`, `docker ps` for lingering rebar-mcp containers
    and their /health `in_flight`, and confirm the retiring container's graceful `docker stop`
    self-drain (_mcp_health.run_http_with_grace) actually completes.
  EOT

  namespace   = "rebar/host"
  metric_name = "mcp_retire_cap"
  statistic   = "Sum"

  # PROFILE B: two observed error periods within four catch intermittent failures.
  # Missing buckets do not count as errors. Treating missing data as breaching would page on
  # ordinary gaps while the counter still reports zero.
  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # PROFILE B: observed errors page. Missing data alone does not.
  # rebar:allow-missing-data-notbreaching: liveness is carried by the dead-man alarms, which keep
  # treat_missing_data = "breaching". The configured window catches observed
  # intermittent errors without false pages when the probe misses a zero.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Epic    = "jira-reb-3527"
    Story   = "panicky-sylphish-foxterrier"
  }
}

resource "aws_cloudwatch_metric_alarm" "mcp_mem_abort" {
  alarm_name        = "rebar-autodeploy-mcp-mem-abort"
  alarm_description = <<-EOT
    The mcp blue-green autodeploy target refused to deploy because the box was below its memory
    floor: on the 8 GiB t4g.large a blue-green overlap briefly DOUBLES the mcp footprint, so the
    second container was not started (AUTODEPLOY_MCP_MEM_ABORT in the rebar-autodeploy.service
    journal; published as rebar/host:mcp_mem_abort by the host probe, observability.sh 4f).
    MEANING: the deploy is fail-safe (the current mcp backend stays live), but a sustained signal
    means the box is memory-bound and mcp is NOT tracking `main`. REMEDIATION: check host memory
    (`free -m`, `journalctl -u rebar-autodeploy`) and what else is resident; a persistent shortfall
    is a box-sizing / footprint problem, not a deploy-loop bug. Kept a DISTINCT metric from
    deploy_errors so a memory-bound stretch does not page the general deploy alarm.
  EOT

  namespace   = "rebar/host"
  metric_name = "mcp_mem_abort"
  statistic   = "Sum"

  # PROFILE B: two observed error periods within four catch intermittent failures.
  # Missing buckets do not count as errors. Treating missing data as breaching would page on
  # ordinary gaps while the counter still reports zero.
  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # PROFILE B: observed errors page. Missing data alone does not.
  # rebar:allow-missing-data-notbreaching: liveness is carried by the dead-man alarms, which keep
  # treat_missing_data = "breaching". The configured window catches observed
  # intermittent errors without false pages when the probe misses a zero.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Epic    = "jira-reb-3527"
    Story   = "panicky-sylphish-foxterrier"
  }
}
