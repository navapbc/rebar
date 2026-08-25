# ---------------------------------------------------------------------------
# monitoring_foxterrier.tf — CloudWatch alarms for the mcp blue-green autodeploy target.
# Story panicky-sylphish-foxterrier (ADR 0079 amendment / 0104), child of epic
# jira-reb-3527 "Enable MCP on AWS".
# ---------------------------------------------------------------------------
# METRIC SOURCE: the on-box systemd oneshot rebar-autodeploy.service (autodeploy.sh) drives the
# mcp target as an immutable-release + atomic-pointer-swap blue-green cutover (build+tag -> start
# NEW container alongside old -> health -> flip the nginx /mcp/ upstream -> retire the old one
# GRACEFULLY when it drains). Two conditions abort/back off without a colliding kill and each
# writes its OWN countable journal marker (kept DISTINCT from AUTODEPLOY_ERROR so a routine
# occurrence never inflates the deploy_errors alarm, exactly as deploy_deferrals is kept separate):
#   * AUTODEPLOY_MCP_RETIRE_CAP — the {8091, blue, green} managed-port pool is exhausted (both
#     blue/green ports held by un-reaped containers still draining); the deploy backed off rather
#     than force-killing a live container (which would kill an in-flight certified op — the
#     review-bot bug 7b4a this design exists to avoid).
#   * AUTODEPLOY_MCP_MEM_ABORT — the 8 GiB t4g.large box was below the memory floor, so the
#     blue-green 2x overlap was refused BEFORE the second container started.
# The host observability probe (infra/scripts/observability.sh §4f) greps the unit journal for
# each token and publishes a per-period count to rebar/host:mcp_retire_cap / rebar/host:mcp_mem_abort
# — the same host-grep, offset-delta shape §4d/§4e use. These alarms watch them.
#
# WHY THEY MATTER: the mcp deploy is fail-safe (the OLD backend stays live and serving, the /mcp/
# upstream is only flipped after the NEW container is healthy), so neither condition is an outage.
# But a SUSTAINED signal means the box cannot converge: mcp_retire_cap => releases are not draining
# (a stuck/never-idle container pins the port pool, so new releases cannot deploy); mcp_mem_abort
# => the box is memory-bound and mcp is not tracking `main`. Investigate the deploy loop
# (`journalctl -u rebar-autodeploy`), the live mcp containers (`docker ps`), and box memory.
#
# Custom metric contract (what the host probe PutMetricData; monitoring_s4b.tf rationale):
#   Namespace  = rebar/host
#   MetricName = mcp_retire_cap / mcp_mem_abort
#   Dimensions = NONE — dimensionless on BOTH sides (a dimension on only one side silently
#                unmatches and the alarm goes permanently INSUFFICIENT_DATA).
#   Unit       = Count  (per-period count of new matching journal lines)
#
# CADENCE: the 900s / 2-of-4 / Sum > 0 / notBreaching shape of monitoring_autodeploy.tf's
# deploy_errors alarm — matched to the deploy's capped backoff (BACKOFF_CAP=900s), so a persistent
# loop latches within ~an hour while a single transient occurrence does not page.
#
# ACTION: wires the shared SNS alerts topic (not a silent alarm), like every alarm above.
# ---------------------------------------------------------------------------

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

  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

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

  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Epic    = "jira-reb-3527"
    Story   = "panicky-sylphish-foxterrier"
  }
}
