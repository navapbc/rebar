# ---------------------------------------------------------------------------
# monitoring_eb6e.tf — CloudWatch alarm for AWS Bedrock InvokeModel client errors.
# Story eb6e (the review-bot Bedrock production cutover).
# ---------------------------------------------------------------------------
# METRIC SOURCE: **AWS-PUBLISHED**, and that is the one structural difference from
# this repo's two sibling alarms. monitoring_s4b.tf (voter_errors) and
# monitoring_s5.tf (replication_errors) both watch metrics the HOST observability
# probe PutMetricData's into the custom `rebar/host` namespace, and both are
# deliberately DIMENSIONLESS ON BOTH SIDES — the probe publishes no dimensions and
# the alarm declares none, because CloudWatch keys a metric by
# namespace+name+dimensions and adding a dimension to only one side makes the alarm
# silently stop matching.
#
# This alarm is the opposite case: `AWS/Bedrock` is a SERVICE namespace that AWS
# populates itself, and AWS publishes `Invocations` / `InvocationClientErrors`
# DIMENSIONED BY `ModelId`. There is no dimensionless roll-up to watch. So the
# correct posture here is to MATCH the dimensioning rather than avoid it: every
# metric block below names `ModelId` explicitly, and the alarm sums across all three
# ids the review-bot actually invokes. Nothing about this alarm involves
# infra/scripts/observability.sh — no custom metric is published for it, and the
# probe must NOT be taught to publish one (that would double-count).
#
# MEASURED (account 896586841071 / us-east-1, recorded on ticket eb6e): the
# `AWS/Bedrock` namespace publishes BOTH `Invocations` and `InvocationClientErrors`
# dimensioned by `ModelId`, and all three of the bot's inference-profile ids already
# have datapoints. The three ids below are the review-bot's model-class slots:
#   frontier -> us.anthropic.claude-opus-4-8
#   standard -> us.anthropic.claude-sonnet-4-6
#   trivial  -> us.anthropic.claude-haiku-4-5-20251001-v1:0
# Summing across all three makes this an alarm on THE GATE, not on one model: a
# 4xx that only breaks the frontier pass still leaves the gate unable to vote, and
# submit REQUIRES the LLM-Review vote (ADR-0013), so it must still fire.
#
# WHY A RATE, NOT A COUNT: `InvocationClientErrors` covers the whole 4xx family —
# AccessDenied (an IAM/inference-profile regression), ValidationException (a model id
# that stopped resolving), ThrottlingException. A handful of these against a busy gate
# is noise; a large FRACTION of invocations failing is the gate going down. The
# cutover ships with NO cross-provider fallback by design, so there is no silent
# degradation mode — either Bedrock answers or the gate stops voting. The
# kill-switch (revert the three REBAR_LLM_*_MODEL class-slot env vars, restoring the
# direct-Anthropic path) is in infra/runbooks/review-bot-ops.md.
#
# WHY `ModelId` MUST STAY IN SYNC: if the class-slot env vars are ever pointed at a
# different Bedrock model id, THIS FILE must be updated too, or the alarm keeps
# watching ids that no longer receive traffic and reports healthy forever. That
# coupling is the price of an AWS-dimensioned metric; it is stated here rather than
# hidden behind a wildcard because a wildcard is not available on an alarm's
# dimensions.
#
# INPUTS: this file introduces none. It reuses three symbols already declared in this
# root module — `var.aws_region` (variables.tf), `data.aws_caller_identity.current`
# (iam.tf), and `aws_sns_topic.alerts` (monitoring.tf) — so an operator-run
# `terraform apply` needs no new variable, secret, or provider.
#
# (monitoring_s4b.tf and monitoring_s5.tf both attribute `var.aws_region` to iam.tf. That
# is wrong — it is declared in variables.tf — and the misattribution is not copied here.)
# ---------------------------------------------------------------------------

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
    from Bedrock to direct Anthropic". Note that llm_retry_max_attempts / timeout_s
    do NOT apply on the Bedrock path (botocore stock client defaults only), so
    tuning them is not a remedy.
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

  # DIVIDE-BY-ZERO / IDLE-GATE GUARD — two independent layers, because an idle gate
  # (no landings for 15 minutes, e.g. overnight) is the common case and must never
  # alarm:
  #
  #   1. `FILL(mN, 0)` on every raw series converts CloudWatch's MISSING datapoints
  #      into 0. AWS emits a Bedrock datapoint only when there is traffic, so an idle
  #      period is a GAP, not a zero — without FILL the sums would be missing rather
  #      than 0 and the arithmetic below would be undefined.
  #   2. `IF(e2 > 0, e1 / e2, 0)` never performs the division when the invocation
  #      denominator is 0. With zero invocations the expression yields 0, which is
  #      below the 0.25 threshold, so the alarm stays OK instead of evaluating 0/0
  #      (which CloudWatch would surface as no datapoint, or — on some math paths —
  #      NaN). This is what makes "idle" and "healthy" the same state here.
  #
  # `treat_missing_data = "notBreaching"` is the third, outermost net: if a period
  # produces no datapoint at all (FILL cannot synthesize points for a series with no
  # data anywhere in the requested range), the period is treated as healthy rather
  # than breaching. Combined with evaluation_periods = 2 this alarm can only fire on
  # real, sustained, measured traffic.
  treat_missing_data = "notBreaching"

  # WIRE SNS IN BOTH DIRECTIONS — deliberate, and load-bearing. An alarm with no actions
  # fires silently, which for this signal is the same as not having it: a Bedrock
  # client-error posture stops the LLM-Review gate voting, so an operator has to be told,
  # and `ok_actions` is what tells them the remedy (the kill-switch, or a throttle
  # passing) actually took. This is the repo's standard pair — see monitoring_autodeploy.tf
  # and monitoring_1fa8.tf, whose comment makes the same "page the operator" call.
  #
  # DO NOT DELETE THESE TO HARMONIZE WITH monitoring_s4b.tf / monitoring_s5.tf. Those two
  # siblings declare no actions at all; that is a known GAP tracked separately, NOT a
  # convention this file should reproduce. If anyone harmonizes, harmonize the other way.
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
