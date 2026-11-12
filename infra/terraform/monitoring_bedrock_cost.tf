# Bedrock prompt-cache and unit-cost regression alarms, per frontier/standard model.
#
# These replace four alarms that were created by hand outside Terraform
# (bedrock-cache-ratio-low-{opus-4-8,sonnet-4-6} and
# bedrock-unit-cost-high-{opus-4-8,sonnet-4-6}) and kept watching the 4.x model ids after
# production moved to the 5.5 inference profiles (bug graceless-thievish-olm). The metric
# math, thresholds, periods and SNS target are carried over unchanged; the per-token rates are
# the AWS on-demand rates for the 5.5 models, which equal the 4.x rates the hand-made alarms
# used. Moving a model is a one-line change to local.bedrock_cost_models; keep its ids aligned
# with REBAR_LLM_*_MODEL and with monitoring_eb6e.tf. The trivial (haiku) slot is not covered:
# the hand-made set had no haiku alarm and there is no unit-cost baseline for it.

locals {
  bedrock_cost_models = {
    opus = {
      model_id = "us.anthropic.claude-opus-5-5"
      # USD per million tokens: uncached input, cache read, output.
      input_rate      = 5.5
      cache_read_rate = 0.55
      output_rate     = 27.5
      # p90 of estimated USD per invocation over 7 days, from the hand-made alarm.
      unit_cost_threshold = 0.09
    }
    sonnet = {
      model_id            = "us.anthropic.claude-sonnet-5-5"
      input_rate          = 3.3
      cache_read_rate     = 0.33
      output_rate         = 16.5
      unit_cost_threshold = 0.07
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "bedrock_cache_ratio_low" {
  for_each = local.bedrock_cost_models

  alarm_name        = "bedrock-cache-ratio-low-${each.key}"
  alarm_description = "Cache-read ratio for ${each.value.model_id} below 80% for 3h while input exceeds 1M tokens/h: possible prompt-caching regression (Aug 2026 class, ~2x cost). Investigate model config and fallback chains."

  # Three consecutive hourly periods; the hourly period lives on each metric block because
  # the top-level period conflicts with metric_query in the AWS provider.
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 0.8
  comparison_operator = "LessThanThreshold"

  # rebar:allow-missing-data-notbreaching: AWS/Bedrock publishes nothing while the model is idle, and an idle model has no caching regression.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.bedrock_spend_cap_alerts.arn]
  ok_actions    = [aws_sns_topic.bedrock_spend_cap_alerts.arn]

  metric_query {
    id          = "ratio"
    expression  = "IF(inp > 1000000, cache / inp, 1)"
    label       = "cache-read ratio"
    return_data = true
  }

  metric_query {
    id = "cache"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "CacheReadInputTokenCount"
      dimensions  = { ModelId = each.value.model_id }
      period      = 3600
      stat        = "Sum"
    }
  }

  metric_query {
    id = "inp"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "InputTokenCount"
      dimensions  = { ModelId = each.value.model_id }
      period      = 3600
      stat        = "Sum"
    }
  }

  tags = {
    Project = "rebar"
  }
}

resource "aws_cloudwatch_metric_alarm" "bedrock_unit_cost_high" {
  for_each = local.bedrock_cost_models

  alarm_name        = "bedrock-unit-cost-high-${each.key}"
  alarm_description = "Estimated cost per invocation for ${each.value.model_id} above the 7-day p90 baseline for 3h: unit-cost drift (caching, context growth, retry inflation, fallback)."

  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = each.value.unit_cost_threshold
  comparison_operator = "GreaterThanThreshold"

  # rebar:allow-missing-data-notbreaching: AWS/Bedrock publishes nothing while the model is idle, and an idle model has no unit cost.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.bedrock_spend_cap_alerts.arn]
  ok_actions    = [aws_sns_topic.bedrock_spend_cap_alerts.arn]

  metric_query {
    id          = "cpi"
    expression  = "IF(inv > 5, (inp*${each.value.input_rate} + cache*${each.value.cache_read_rate} + out*${each.value.output_rate})/1000000/inv, 0)"
    label       = "est. USD per invocation"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = {
      inp   = "InputTokenCount"
      cache = "CacheReadInputTokenCount"
      out   = "OutputTokenCount"
      inv   = "Invocations"
    }
    content {
      id = metric_query.key
      metric {
        namespace   = "AWS/Bedrock"
        metric_name = metric_query.value
        dimensions  = { ModelId = each.value.model_id }
        period      = 3600
        stat        = "Sum"
      }
    }
  }

  tags = {
    Project = "rebar"
  }
}
