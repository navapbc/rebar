# AWS/Bedrock publishes Invocations and InvocationClientErrors by ModelId.
# Sum the review bot's frontier, standard, and trivial Claude slots, then alarm
# on client-error rate rather than raw count. Keep all three ModelId dimensions
# aligned with REBAR_LLM_*_MODEL settings. AWS has no dimensionless roll-up.
# The host probe does not publish this service metric. Notify the shared SNS
# topic when a model-specific regression blocks the LLM-Review vote.

resource "aws_cloudwatch_metric_alarm" "bedrock_invoke_client_errors" {
  alarm_name        = "rebar-bedrock-invoke-client-errors"
  alarm_description = <<-EOT
    AWS Bedrock InvokeModel client-error (4xx) rate above 25% of invocations,
    summed across the review-bot's three model-class slots (opus / sonnet / haiku
    inference profiles) over two consecutive 15-minute periods. Sourced from the
    AWS-published AWS/Bedrock namespace, dimensioned by ModelId.

    InvocationClientErrors is the 4xx family: AccessDenied (an IAM or
    inference-profile regression), ValidationException (a model id that stopped
    resolving), ThrottlingException. The Bedrock cutover ships with NO
    cross-provider fallback, so a sustained client-error rate means the LLM-Review
    voter cannot vote and — because submit requires that vote (ADR-0013) — changes
    stack up unsubmittable.

    Runbook: infra/runbooks/review-bot-ops.md, "Kill-switch: revert the LLM path
    from Bedrock to direct Anthropic". llm_retry_max_attempts / the configured
    timeout DO apply on the Bedrock path (wired into the botocore client Config,
    bug 61d8), so tuning them is a real remedy alongside the kill-switch.
  EOT

  # 15-minute periods, two in a row. A 4xx spike inside one 15-minute window is
  # usually a transient throttle; two consecutive breaching windows is a posture,
  # not a blip. NOTE the 900-second period lives on each `metric` block below, NOT
  # here: the top-level `period` argument CONFLICTS with `metric_query` in the AWS
  # provider (it is the single-metric form), so a metric-math alarm carries its
  # period per raw series. All six declare 900 so the math aligns period-for-period.
  evaluation_periods  = 2
  threshold           = 0.25
  comparison_operator = "GreaterThanThreshold"

  # FILL maps sparse Bedrock series to zero. IF avoids dividing by zero when
  # the gate is idle. An entirely absent period also stays notBreaching, so
  # only measured client-error rates over requests can page.
  treat_missing_data = "notBreaching"

  # Notify SNS on failure and recovery. A sustained client-error rate can block
  # LLM-Review voting and requires an operator response.
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  # --- Numerator: client errors summed across all three model ids ----------
  metric_query {
    id          = "e1"
    expression  = "SUM([FILL(m1,0),FILL(m2,0),FILL(m3,0)])"
    label       = "InvocationClientErrors (all three review-bot model ids)"
    return_data = false
  }

  # --- Denominator: invocations summed across the same three model ids ------
  metric_query {
    id          = "e2"
    expression  = "SUM([FILL(m4,0),FILL(m5,0),FILL(m6,0)])"
    label       = "Invocations (all three review-bot model ids)"
    return_data = false
  }

  # --- The alarming expression: the guarded rate ----------------------------
  metric_query {
    id          = "e3"
    expression  = "IF(e2 > 0, e1 / e2, 0)"
    label       = "Bedrock client-error rate (0 when idle)"
    return_data = true
  }

  # --- Raw AWS/Bedrock series. ModelId is declared EXPLICITLY on each; see the
  # --- METRIC SOURCE note above on why matching AWS's dimensioning is required.
  metric_query {
    id = "m1"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "InvocationClientErrors"
      dimensions  = { ModelId = "us.anthropic.claude-opus-4-8" }
      period      = 900
      stat        = "Sum"
    }
  }

  metric_query {
    id = "m2"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "InvocationClientErrors"
      dimensions  = { ModelId = "us.anthropic.claude-sonnet-4-6" }
      period      = 900
      stat        = "Sum"
    }
  }

  metric_query {
    id = "m3"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "InvocationClientErrors"
      dimensions  = { ModelId = "us.anthropic.claude-haiku-4-5-20251001-v1:0" }
      period      = 900
      stat        = "Sum"
    }
  }

  metric_query {
    id = "m4"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "Invocations"
      dimensions  = { ModelId = "us.anthropic.claude-opus-4-8" }
      period      = 900
      stat        = "Sum"
    }
  }

  metric_query {
    id = "m5"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "Invocations"
      dimensions  = { ModelId = "us.anthropic.claude-sonnet-4-6" }
      period      = 900
      stat        = "Sum"
    }
  }

  metric_query {
    id = "m6"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "Invocations"
      dimensions  = { ModelId = "us.anthropic.claude-haiku-4-5-20251001-v1:0" }
      period      = 900
      stat        = "Sum"
    }
  }

  tags = {
    Project = "rebar"
    Story   = "eb6e"
  }
}

# Discoverability anchor: the alarm's metrics are account/region-scoped under this
# identity (mirrors monitoring_s4b.tf / monitoring_s5.tf; makes region/account
# provenance explicit and matches iam.tf's usage). The MEASURED datapoints backing
# the ModelId dimensions above were observed in exactly this account+region.
locals {
  bedrock_alarm_region     = var.aws_region
  bedrock_alarm_account_id = data.aws_caller_identity.current.account_id
}
