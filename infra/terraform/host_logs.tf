# Host logs survive the host (bug 9018-3385-ccfa-4568, epic 80c4-dcb6-bb87-4810).
#
# The 2026-09-04 outage left no evidence: the host's logs lived only in on-box journald, every
# health signal except the EC2 status checks was published BY the host, and the restart that
# restored service destroyed the only record. Everything here is AWS-managed and declared in
# Terraform. The repo ships no installer, no package download and no version pin:
#
#   - State Manager installs the CloudWatch agent (Distributor package, version unpinned, so
#     AWS keeps it current) and applies its config from an SSM parameter, both on a schedule
#     against the Name tag, so a rebuilt host is covered with no manual step.
#   - The agent ships journald (kernel, docker.service = every compose container, rebar-* units,
#     nginx, systemd unit-failure lines) plus nginx error.log to one log group with explicit
#     retention. access.log is deliberately not shipped.
#   - An IncomingLogEvents alarm pages when shipping stops, so a dead pipeline cannot pass for a
#     configured one.
#   - A Route 53 health check observes Gerrit from OUTSIDE the host.
#
# Runbook: infra/runbooks/gerrit-host-wedged-ssm-lost.md ("Preserve evidence" and "Read the
# host's logs without the host").

locals {
  host_log_group_name       = "/rebar/gerrit-host"
  host_cwagent_config_param = "AmazonCloudWatch-rebar-gerrit-host"
  host_name_tag             = "rebar-gerrit"
}

resource "aws_cloudwatch_log_group" "gerrit_host" {
  name = local.host_log_group_name
  # Explicit and finite: the account's other groups default to never-expire. 90 days matches
  # bedrock_spend_cap.tf and monitoring_lmn_private_context.tf. The agent config sets NO
  # retention_in_days, so Terraform is the single owner of this value.
  retention_in_days = 90

  tags = {
    Project = "rebar"
    Ticket  = "9018-3385-ccfa-4568"
  }
}

# The agent config. The AmazonCloudWatch- prefix is the namespace AWS's own
# CloudWatchAgentServerPolicy grants reads on; the role policy below grants exactly this ARN.
# The JSON's log_group_name must equal local.host_log_group_name
# (tests/infra/test_host_logs_offbox.py enforces it).
resource "aws_ssm_parameter" "cwagent_config" {
  name        = local.host_cwagent_config_param
  description = "CloudWatch agent config for the rebar Gerrit host (journald + nginx error.log)."
  type        = "String"
  value       = file("${path.module}/cwagent/rebar-host.json")

  tags = {
    Project = "rebar"
    Ticket  = "9018-3385-ccfa-4568"
  }
}

# Install. An Install at the version already present is a no-op success (amazon-ssm-agent
# configurepackage checkAlreadyInstalled), so the daily run only acts when AWS publishes a new
# default version or the host was rebuilt. It runs at 08:50 UTC, ten minutes before the hourly
# configure run below, so a reinstall is re-configured inside the 15-minute window of
# host_logs_not_arriving.
resource "aws_ssm_association" "cwagent_install" {
  association_name    = "rebar-gerrit-cwagent-install"
  name                = "AWS-ConfigureAWSPackage"
  schedule_expression = "cron(50 8 ? * * *)"

  parameters = {
    action = "Install"
    name   = "AmazonCloudWatchAgent"
  }

  targets {
    key    = "tag:Name"
    values = [local.host_name_tag]
  }
}

# Configure. Re-applying hourly restarts the agent once an hour; journald collection resumes
# from its saved cursor and file collection from its saved offset, so a restart loses nothing.
resource "aws_ssm_association" "cwagent_configure" {
  association_name    = "rebar-gerrit-cwagent-configure"
  name                = "AmazonCloudWatch-ManageAgent"
  schedule_expression = "cron(0 0/1 * * ? *)"

  parameters = {
    action                        = "configure"
    mode                          = "ec2"
    optionalConfigurationSource   = "ssm"
    optionalConfigurationLocation = aws_ssm_parameter.cwagent_config.name
    optionalRestart               = "yes"
  }

  targets {
    key    = "tag:Name"
    values = [local.host_name_tag]
  }
}

# The agent's log and config reads, scoped to this one group and this one parameter. The
# actions mirror AWS's CloudWatchAgentServerPolicy minus metrics, X-Ray and retention (the agent
# never sets retention here). The group already exists, so no CreateLogGroup is needed.
data "aws_iam_policy_document" "host_logs_ship" {
  statement {
    sid = "ShipHostLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      aws_cloudwatch_log_group.gerrit_host.arn,
      "${aws_cloudwatch_log_group.gerrit_host.arn}:*",
    ]
  }

  statement {
    sid       = "ReadAgentConfig"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.cwagent_config.arn]
  }
}

resource "aws_iam_role_policy" "host_logs_ship" {
  name   = "rebar-gerrit-host-logs-ship"
  role   = aws_iam_role.gerrit_instance.id
  policy = data.aws_iam_policy_document.host_logs_ship.json
}

# Shipping stopped. The journal carries ~190 entries per 5 minutes on a healthy host (2026-11-03
# survey), and the 5-minute probe alone logs every tick, so a 15-minute empty window means the
# agent, its config or the host is gone. AWS publishes IncomingLogEvents only when events
# arrive, so silence is missing data, and missing data must page.
resource "aws_cloudwatch_metric_alarm" "host_logs_not_arriving" {
  alarm_name        = "rebar-host-logs-not-arriving"
  alarm_description = <<-EOT
    The rebar Gerrit host has delivered no log events to CloudWatch Logs group
    /rebar/gerrit-host for 3 of 3 five-minute periods. Either the host is down, or the
    CloudWatch agent stopped, lost its config or lost permission. Host logs are then
    being kept ONLY on the box again. TRIAGE: `aws ssm describe-instance-associations-status`
    for the cwagent install/configure associations, then on the host
    `systemctl status amazon-cloudwatch-agent`. Runbook:
    infra/runbooks/gerrit-host-wedged-ssm-lost.md.
  EOT

  namespace   = "AWS/Logs"
  metric_name = "IncomingLogEvents"
  statistic   = "Sum"

  dimensions = {
    LogGroupName = aws_cloudwatch_log_group.gerrit_host.name
  }

  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "9018-3385-ccfa-4568"
  }
}

# The one host-health observation that does not originate on the host. Route 53's checkers
# fetch the same endpoint the host probe uses for Rebar/Gate:GerritReachable, from outside AWS's
# view of the box, so a dead host cannot silence it.
resource "aws_route53_health_check" "gerrit_https" {
  type              = "HTTPS"
  fqdn              = var.dns_name
  port              = 443
  resource_path     = "/config/server/version"
  request_interval  = 30
  failure_threshold = 3

  tags = {
    Name    = "rebar-gerrit-https"
    Project = "rebar"
    Ticket  = "9018-3385-ccfa-4568"
  }
}

# Route 53 health-check metrics are published only in us-east-1, which is this stack's region.
resource "aws_cloudwatch_metric_alarm" "gerrit_external_probe_down" {
  alarm_name        = "rebar-gerrit-external-probe-down"
  alarm_description = <<-EOT
    Route 53 health checkers OUTSIDE the host cannot get HTTP 2xx/3xx from
    https://${var.dns_name}/config/server/version for 3 of 3 one-minute periods. This
    signal does not depend on the host publishing anything, so it still reports when
    the host's own probe and metrics have gone silent. Before any restart, capture
    evidence first: infra/runbooks/gerrit-host-wedged-ssm-lost.md.
  EOT

  namespace   = "AWS/Route53"
  metric_name = "HealthCheckStatus"
  statistic   = "Minimum"

  dimensions = {
    HealthCheckId = aws_route53_health_check.gerrit_https.id
  }

  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "9018-3385-ccfa-4568"
  }
}
