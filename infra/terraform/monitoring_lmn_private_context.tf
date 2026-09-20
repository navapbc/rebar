# ---------------------------------------------------------------------------
# monitoring_lmn_private_context.tf — lmn-staging private-context deny signal
# Ticket 794e-7479-5f21-49db.
# ---------------------------------------------------------------------------
#
# The previous live alarm used raw AWS/S3 4xxErrors for
# lmn-staging-private-context-896586841071 / all-private-context-requests. That
# metric counts the expected negative-access probes from
# lmn-staging-private-audit-* sessions, so it paged on proof that isolation was
# working. On 2026-09-18, the worst 5-minute windows held 49 and 45 403s, with
# 45 audit denials in each; the audit floor alone clears the old Sum > 5
# threshold nine times over. Do NOT fix this by raising the threshold (that hides
# real denials behind a moving audit floor) or by adding the audited application
# roles to the bucket policy allowlist; the policy is the control being audited.
#
# Metric source: S3 server access logs delivered under
# s3://lmn-staging-private-context-logs-896586841071/access/. The account has no
# CloudTrail trail or event data store, so S3 data-event denials are not visible
# there today. Server access logs are delayed best-effort delivery, so this
# alarm detects the burst after delivery rather than at request time.
#
# Counted records:
#   bucket      = lmn-staging-private-context-896586841071
#   http_status = 403
#   error_code  = AccessDenied
#   requester NOT LIKE arn:aws:sts::896586841071:assumed-role/*/lmn-staging-private-audit-*
#   requester  != arn:aws:iam::896586841071:user/joe_frontier
#
# The CloudWatch alarm keeps the existing alarm name so the operator-facing
# signal stays stable while the metric source becomes selective.
# ---------------------------------------------------------------------------

locals {
  lmn_private_context_bucket      = "lmn-staging-private-context-896586841071"
  lmn_private_context_log_bucket  = "lmn-staging-private-context-logs-896586841071"
  lmn_private_context_log_prefix  = "access/"
  lmn_private_context_alarm_name  = "lmn-staging-private-context-denied"
  lmn_private_context_metric_ns   = "lmn/staging"
  lmn_private_context_metric_name = "private_context_unexpected_denials"
}

data "aws_s3_bucket" "lmn_private_context_logs" {
  bucket = local.lmn_private_context_log_bucket
}

data "archive_file" "lmn_private_context_denied" {
  type        = "zip"
  output_path = format("%s/lmn-private-context-denied/dist/lmn-private-context-denied.zip", path.module)

  source {
    content  = file(format("%s/lmn-private-context-denied/handler.py", path.module))
    filename = "handler.py"
  }
}

resource "aws_iam_role" "lmn_private_context_denied" {
  name        = "lmn-private-context-denied-metric"
  description = "Processes lmn-staging private-context S3 access logs into unexpected-denial metrics"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Project = "rebar"
    Ticket  = "794e"
  }
}

resource "aws_iam_role_policy" "lmn_private_context_denied" {
  name = "lmn-private-context-denied-metric"
  role = aws_iam_role.lmn_private_context_denied.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Logs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = format(
          "arn:aws:logs:%s:%s:log-group:/aws/lambda/lmn-private-context-denied-metric:*",
          var.aws_region,
          data.aws_caller_identity.current.account_id,
        )
      },
      {
        Sid      = "ListDeliveredAccessLogs"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = data.aws_s3_bucket.lmn_private_context_logs.arn
        Condition = {
          StringLike = { "s3:prefix" = format("%s*", local.lmn_private_context_log_prefix) }
        }
      },
      {
        Sid      = "ReadDeliveredAccessLogs"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = format("arn:aws:s3:::%s/%s*", local.lmn_private_context_log_bucket, local.lmn_private_context_log_prefix)
      },
      {
        Sid      = "PublishUnexpectedDenialMetric"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = local.lmn_private_context_metric_ns }
        }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "lmn_private_context_denied" {
  name              = "/aws/lambda/lmn-private-context-denied-metric"
  retention_in_days = 90

  tags = {
    Project = "rebar"
    Ticket  = "794e"
  }
}

resource "aws_lambda_function" "lmn_private_context_denied" {
  function_name    = "lmn-private-context-denied-metric"
  description      = "Counts unexpected AccessDenied responses from lmn-staging private-context S3 access logs"
  role             = aws_iam_role.lmn_private_context_denied.arn
  runtime          = format("python%s", join(".", ["3", "12"]))
  handler          = "handler.handler"
  filename         = data.archive_file.lmn_private_context_denied.output_path
  source_code_hash = data.archive_file.lmn_private_context_denied.output_base64sha256
  timeout          = 60
  memory_size      = 256

  depends_on = [aws_cloudwatch_log_group.lmn_private_context_denied]

  tags = {
    Project = "rebar"
    Ticket  = "794e"
  }
}

resource "aws_cloudwatch_event_rule" "lmn_private_context_denied_tick" {
  name                = "lmn-private-context-denied-metric-tick"
  description         = "Scans newly delivered lmn-staging private-context S3 access logs for unexpected denials"
  schedule_expression = "rate(5 minutes)"

  tags = {
    Project = "rebar"
    Ticket  = "794e"
  }
}

resource "aws_cloudwatch_event_target" "lmn_private_context_denied_tick" {
  rule      = aws_cloudwatch_event_rule.lmn_private_context_denied_tick.name
  target_id = "lambda"
  arn       = aws_lambda_function.lmn_private_context_denied.arn
}

resource "aws_lambda_permission" "lmn_private_context_denied_tick" {
  statement_id  = "events-tick"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lmn_private_context_denied.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lmn_private_context_denied_tick.arn
}

resource "aws_cloudwatch_metric_alarm" "lmn_private_context_denied" {
  alarm_name        = local.lmn_private_context_alarm_name
  alarm_description = <<-EOT
    Unexpected lmn-staging private-context S3 AccessDenied responses exceeded
    5 in one 300-second period. Sourced from S3 server access logs at
    s3://${local.lmn_private_context_log_bucket}/${local.lmn_private_context_log_prefix}
    and excludes expected lmn-staging-private-audit-* negative-access probes
    plus arn:aws:iam::896586841071:user/joe_frontier.
  EOT

  namespace   = local.lmn_private_context_metric_ns
  metric_name = local.lmn_private_context_metric_name
  statistic   = "Sum"

  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "794e"
  }
}
