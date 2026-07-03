# ---------------------------------------------------------------------------
# monitoring_autodeploy.tf — CloudWatch alarm for continuous-auto-deploy failures.
# Epic 88ab / story 8903.
# ---------------------------------------------------------------------------
# METRIC SOURCE: the on-box systemd oneshot rebar-autodeploy.service (autodeploy.sh) writes
# a structured `AUTODEPLOY_ERROR` marker to stderr -> journald on any deploy-step failure
# (git fetch, config-check, materialise, review-bot build/health-check-then-rollback). The
# HOST observability probe (infra/scripts/observability.sh §4d) greps the unit journal for
# those markers and publishes a per-period count to rebar/host:deploy_errors — the same
# host-grep pattern §4/§4c use for voter_errors / merge-change errors. This alarm watches it.
#
# WHY IT MATTERS: a persistent signal means the box is NOT tracking `main` (config/code
# drift) and/or a deploy keeps failing and backing off. The auto-deploy is fail-safe (the
# last-known-good review-bot + config stay live, so the gate is NOT frozen), but sustained
# failure means fixes to `main` are not reaching production — investigate the deploy loop
# (journalctl -u rebar-autodeploy) and the target `main` tip.
#
# Custom metric contract (what the host probe must PutMetricData):
#   Namespace  = rebar/host
#   MetricName = deploy_errors
#   Dimensions = NONE — dimensionless on BOTH sides (see monitoring_s4b.tf rationale).
#   Unit       = Count   (per-period count of new AUTODEPLOY_ERROR journal lines)
#
# ACTION: wires the shared SNS alerts topic (not a silent alarm), like WS7 / 1fa8 / §4c.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "deploy_errors" {
  alarm_name        = "rebar-autodeploy-errors"
  alarm_description = <<-EOT
    rebar continuous auto-deploy failures detected in the rebar-autodeploy.service journal
    (AUTODEPLOY_ERROR markers: fetch / config-check / materialise / review-bot build or
    health-check-then-rollback). Published as rebar/host:deploy_errors by the host
    observability probe (§4d). The deploy is fail-safe (last-known-good stays live, gate not
    frozen), but a sustained signal means `main` is not reaching the box — investigate
    `journalctl -u rebar-autodeploy` and the target main tip. Disable via
    `systemctl disable --now rebar-autodeploy.timer`; the manual deploy path still works.
  EOT

  namespace   = "rebar/host"
  metric_name = "deploy_errors"
  statistic   = "Sum"

  # 5-minute periods; the deploy backs off, so alarm on 2 periods to avoid a single-transient
  # page while still catching a stuck/looping-failure deploy.
  period              = 300
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Epic    = "88ab"
    Story   = "8903"
  }
}
