# ---------------------------------------------------------------------------
# bedrock_spend_cap.tf — hard $500/day cap on Amazon Bedrock spend
# ---------------------------------------------------------------------------
#
# WHY THIS IS NOT JUST AN AWS BUDGET WITH AN ACTION
#
# The obvious implementation — a DAILY budget with an APPLY_IAM_POLICY action —
# is not available. AWS Budgets rejects it outright:
#
#   InvalidParameterException: AWS Budgets Actions don't support daily
#   granularity budget for now.
#
# MONTHLY is the finest period that can auto-apply a deny, and a monthly budget
# cannot express "$500 today" — a single runaway day inside the month sails
# through untouched. There is no native AWS mechanism for a hard per-day dollar
# cap on Bedrock, so the enforcement lives in a Lambda (see
# bedrock-spend-cap/handler.py for the metering design).
#
# The DAILY budget below is still created, but it only ALERTS. It is the
# human-visible tripwire; aws_lambda_function.bedrock_spend_cap is the one that
# actually stops spend.
#
# ADOPTING THE LIVE RESOURCES
#
# This stack was deployed via the CLI first so the cap was protecting the
# account the same day, then reconciled into Terraform. The `import` blocks at
# the bottom adopt those live resources — the first `terraform apply` binds
# them to state rather than trying to create duplicates and failing on
# EntityAlreadyExists. Delete the import blocks once the apply has run.
# ---------------------------------------------------------------------------

variable "bedrock_daily_cap_usd" {
  type        = number
  description = <<-EOT
    Hard ceiling on Amazon Bedrock spend per UTC day, in USD.

    Deliberately far above normal usage: observed Bedrock spend on this account
    is cents per day, so this is a runaway guard, not a throttle for routine
    work. For scale, $500 buys roughly 151M Claude Sonnet 4.6 input tokens or
    30M output tokens at Bedrock regional rates — a workload that could only
    occur by accident or abuse.

    Lowering this below normal daily spend would deny production Bedrock calls
    during ordinary operation. Change it with that in mind.
  EOT
  default     = 500
}

variable "bedrock_cap_target_roles" {
  type        = list(string)
  description = <<-EOT
    IAM roles that lose Bedrock access when the daily cap is breached.

    This is every role in the account whose policies grant a Bedrock
    invocation action. A role missing from this list is a hole in the cap: it
    keeps spending after the cap trips. Re-derive the list when Bedrock
    permissions are granted to a new role.

    BedrockInvocationLoggingRole is deliberately ABSENT. It is the role Bedrock
    assumes to write invocation logs — it spends nothing, and denying it would
    blind the audit trail at exactly the moment it matters most.
  EOT
  default = [
    "lmn-api",
    "lmn-doc-extractor",
    "lmn-doc-indexer",
    "lmn-report-generator",
    "lmn-rebar-code-review",
    "lmn-staging-api",
    "lmn-staging-doc-extractor",
    "lmn-staging-doc-indexer",
    "lmn-staging-report-generator",
    "rebar-external-ci-bedrock",
    "rebar-gerrit-instance-role",
  ]
}

variable "bedrock_cap_target_groups" {
  type        = list(string)
  description = <<-EOT
    IAM groups that lose Bedrock access when the daily cap is breached.

    Admin is included because a cap with a hole in it is not a cap: ad-hoc
    runaway spend usually comes from a human running a script under their own
    admin credentials, not from a service role. The deny covers Bedrock
    invocation only, so an admin retains every permission needed to inspect
    and lift the cap.
  EOT
  default     = ["Admin"]
}

locals {
  bedrock_cap_account_id = data.aws_caller_identity.current.account_id
  bedrock_cap_name       = "bedrock-spend-cap"

  # Published Bedrock $/1K-token rates, consulted ONLY for models with no
  # billing history on this account (the Lambda derives real rates from actual
  # bills wherever it can — see handler.py). Seeded at the regional-endpoint
  # rate, ~10% above global, so the seed errs high rather than low.
  bedrock_cap_seed_rates = {
    "claude-haiku-4-5"  = { input = 0.0011, output = 0.0055 }
    "claude-sonnet-4-6" = { input = 0.0033, output = 0.0165 }
    "claude-opus"       = { input = 0.0055, output = 0.0275 }
  }

  # Titan embeddings bill as "TitanEmbeddingV2-Text" but meter as
  # "amazon.titan-embed-text-v2:0"; the two do not normalise to each other, so
  # the mapping has to be stated. Without this pin, embedding tokens would be
  # priced at the fail-closed fallback and trip the cap far too early.
  bedrock_cap_rate_overrides = {
    "titan-embed-text" = "TitanEmbeddingV2-Text"
  }
}

# --- the deny applied when the cap trips ------------------------------------
# Scoped to the actions that SPEND. Management and read-only calls stay allowed
# on purpose: whoever is diagnosing a tripped cap needs to be able to look at
# Bedrock while it is tripped.
resource "aws_iam_policy" "bedrock_spend_cap_deny" {
  name        = "BedrockDailySpendCapDeny"
  description = "Attached when Bedrock spend exceeds the daily cap. Detached automatically at 00:00 UTC."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "DenyBedrockSpendWhenDailyCapExceeded"
      Effect = "Deny"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
        "bedrock:StartAsyncInvoke",
        "bedrock:InvokeAgent",
        "bedrock:InvokeInlineAgent",
        "bedrock:InvokeFlow",
        "bedrock:Retrieve",
        "bedrock:RetrieveAndGenerate",
        "bedrock:ApplyGuardrail",
        "bedrock:CreateModelInvocationJob",
        "bedrock:CreateModelCustomizationJob",
      ]
      Resource = "*"
    }]
  })

  tags = {
    Project = "rebar"
  }
}

# --- alerting ---------------------------------------------------------------
resource "aws_sns_topic" "bedrock_spend_cap_alerts" {
  name = "bedrock-spend-cap-alerts"

  tags = {
    Project = "rebar"
  }
}

# Subscriptions are intentionally NOT managed here. Every endpoint type worth
# using (email, chatbot) requires an out-of-band confirmation click, so a
# Terraform-managed subscription sits in "pending confirmation" and reports
# success while delivering nothing.
resource "aws_sns_topic_policy" "bedrock_spend_cap_alerts" {
  arn = aws_sns_topic.bedrock_spend_cap_alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "bedrock-spend-cap-alerts-policy"
    Statement = [
      {
        Sid       = "AllowOwnerFullControl"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.bedrock_cap_account_id}:root" }
        # SNS topic policies reject the `SNS:*` wildcard ("action out of service
        # scope"); the permitted actions have to be spelled out.
        Action = [
          "SNS:GetTopicAttributes", "SNS:SetTopicAttributes", "SNS:AddPermission",
          "SNS:RemovePermission", "SNS:DeleteTopic", "SNS:Subscribe",
          "SNS:ListSubscriptionsByTopic", "SNS:Publish",
        ]
        Resource = aws_sns_topic.bedrock_spend_cap_alerts.arn
      },
      {
        Sid       = "AllowBudgetsPublish"
        Effect    = "Allow"
        Principal = { Service = "budgets.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.bedrock_spend_cap_alerts.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.bedrock_cap_account_id }
        }
      },
    ]
  })
}

# --- the alert-only daily budget -------------------------------------------
# No budget ACTION is attached, and none can be: see the header comment.
resource "aws_budgets_budget" "bedrock_daily" {
  name         = "bedrock-daily-500"
  budget_type  = "COST"
  limit_amount = tostring(var.bedrock_daily_cap_usd)
  limit_unit   = "USD"
  time_unit    = "DAILY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Bedrock"]
  }

  # Credits are excluded so the budget tracks what was actually consumed. With
  # credits applied, a runaway burning down a credit balance would register as
  # $0 spend and the alert would never fire.
  cost_types {
    include_credit             = false
    include_refund             = false
    include_tax                = true
    include_subscription       = true
    include_upfront            = true
    include_recurring          = true
    include_other_subscription = true
    include_support            = true
    include_discount           = true
    use_amortized              = false
    use_blended                = false
  }

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_sns_topic_arns  = [aws_sns_topic.bedrock_spend_cap_alerts.arn]
    }
  }
}

# --- enforcement Lambda -----------------------------------------------------
data "archive_file" "bedrock_spend_cap" {
  type        = "zip"
  output_path = "${path.module}/bedrock-spend-cap/dist/bedrock-spend-cap.zip"

  source {
    content  = file("${path.module}/bedrock-spend-cap/handler.py")
    filename = "handler.py"
  }
}

resource "aws_iam_role" "bedrock_spend_cap" {
  name        = "${local.bedrock_cap_name}-lambda"
  description = "Execution role for the bedrock-spend-cap enforcement Lambda"

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
  }
}

resource "aws_iam_role_policy" "bedrock_spend_cap" {
  name = local.bedrock_cap_name
  role = aws_iam_role.bedrock_spend_cap.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${local.bedrock_cap_account_id}:log-group:/aws/lambda/${local.bedrock_cap_name}:*"
      },
      {
        Sid    = "ReadSpendSignals"
        Effect = "Allow"
        # None of these support resource-level permissions.
        Action = [
          "ce:GetCostAndUsage",
          "cloudwatch:ListMetrics",
          "cloudwatch:GetMetricData",
        ]
        Resource = "*"
      },
      {
        Sid      = "State"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = "arn:aws:ssm:${var.aws_region}:${local.bedrock_cap_account_id}:parameter/${local.bedrock_cap_name}/state"
      },
      {
        Sid      = "Notify"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.bedrock_spend_cap_alerts.arn
      },
      {
        Sid      = "ReadAttachmentState"
        Effect   = "Allow"
        Action   = ["iam:ListAttachedRolePolicies", "iam:ListAttachedGroupPolicies"]
        Resource = "*"
      },
      {
        # The iam:PolicyARN condition is the load-bearing part. Without it this
        # role could attach ANY policy — including AdministratorAccess — to
        # every principal listed below, turning a cost control into a
        # privilege-escalation path. Verified with
        # `aws iam simulate-principal-policy`: attaching the deny policy is
        # allowed, attaching AdministratorAccess is implicitly denied.
        Sid    = "ApplyOnlyTheDenyPolicyOnlyToCapTargets"
        Effect = "Allow"
        Action = [
          "iam:AttachRolePolicy", "iam:DetachRolePolicy",
          "iam:AttachGroupPolicy", "iam:DetachGroupPolicy",
        ]
        Resource = concat(
          [for r in var.bedrock_cap_target_roles :
          "arn:aws:iam::${local.bedrock_cap_account_id}:role/${r}"],
          [for g in var.bedrock_cap_target_groups :
          "arn:aws:iam::${local.bedrock_cap_account_id}:group/${g}"],
        )
        Condition = {
          ArnEquals = { "iam:PolicyARN" = aws_iam_policy.bedrock_spend_cap_deny.arn }
        }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "bedrock_spend_cap" {
  name              = "/aws/lambda/${local.bedrock_cap_name}"
  retention_in_days = 90

  tags = {
    Project = "rebar"
  }
}

resource "aws_lambda_function" "bedrock_spend_cap" {
  function_name    = local.bedrock_cap_name
  description      = "Hard per-UTC-day dollar cap on Amazon Bedrock spend"
  role             = aws_iam_role.bedrock_spend_cap.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.bedrock_spend_cap.output_path
  source_code_hash = data.archive_file.bedrock_spend_cap.output_base64sha256

  # Generous for what is normally a sub-second run: a cold start that also has
  # to page through every AWS/Bedrock metric in two regions is the slow case,
  # and a timeout here means the cap silently stops being enforced.
  timeout     = 120
  memory_size = 512

  environment {
    variables = {
      THRESHOLD_USD   = tostring(var.bedrock_daily_cap_usd)
      DENY_POLICY_ARN = aws_iam_policy.bedrock_spend_cap_deny.arn
      TARGET_ROLES    = join(",", var.bedrock_cap_target_roles)
      TARGET_GROUPS   = join(",", var.bedrock_cap_target_groups)

      # us-west-2 has no Bedrock metrics today, but the frontier-bedrock profile
      # points there, so it is watched pre-emptively. An unwatched region is
      # unmetered spend.
      REGIONS = "us-east-1,us-west-2"

      STATE_PARAM   = "/${local.bedrock_cap_name}/state"
      SNS_TOPIC_ARN = aws_sns_topic.bedrock_spend_cap_alerts.arn

      # Cost Explorer bills $0.01 per request. Polling it on the 5-minute tick
      # would cost ~$172/mo — an absurd way to run a cost control — so CE is
      # rate-limited to hourly and the fast path rides on CloudWatch.
      CE_MIN_INTERVAL_SECONDS = "3600"

      RATE_WINDOW_DAYS = "60"

      # $75/1M tokens: above any current Bedrock rate, so a model with neither
      # billing history nor a seed price trips the cap early instead of
      # slipping under it. Failing closed is the point of a cap.
      UNKNOWN_RATE_PER_1K = "0.075"

      RATE_OVERRIDES = jsonencode(local.bedrock_cap_rate_overrides)
      SEED_RATES     = jsonencode(local.bedrock_cap_seed_rates)
      DRY_RUN        = "false"
    }
  }

  depends_on = [aws_cloudwatch_log_group.bedrock_spend_cap]

  tags = {
    Project = "rebar"
  }
}

# --- the tick ---------------------------------------------------------------
# Five minutes is the enforcement granularity: it bounds how much a runaway can
# spend between the breach and the deny. This same rule performs the 00:00 UTC
# release, so no second schedule is needed.
resource "aws_cloudwatch_event_rule" "bedrock_spend_cap_tick" {
  name                = "${local.bedrock_cap_name}-tick"
  description         = "Drives the Bedrock daily spend cap; also performs the 00:00 UTC release"
  schedule_expression = "rate(5 minutes)"

  tags = {
    Project = "rebar"
  }
}

resource "aws_cloudwatch_event_target" "bedrock_spend_cap_tick" {
  rule      = aws_cloudwatch_event_rule.bedrock_spend_cap_tick.name
  target_id = "lambda"
  arn       = aws_lambda_function.bedrock_spend_cap.arn
}

resource "aws_lambda_permission" "bedrock_spend_cap_tick" {
  statement_id  = "events-tick"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.bedrock_spend_cap.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.bedrock_spend_cap_tick.arn
}

# --- adopt the already-deployed resources (delete after the first apply) -----
import {
  to = aws_iam_policy.bedrock_spend_cap_deny
  id = "arn:aws:iam::896586841071:policy/BedrockDailySpendCapDeny"
}

import {
  to = aws_sns_topic.bedrock_spend_cap_alerts
  id = "arn:aws:sns:us-east-1:896586841071:bedrock-spend-cap-alerts"
}

import {
  to = aws_sns_topic_policy.bedrock_spend_cap_alerts
  id = "arn:aws:sns:us-east-1:896586841071:bedrock-spend-cap-alerts"
}

import {
  to = aws_budgets_budget.bedrock_daily
  id = "896586841071:bedrock-daily-500"
}

import {
  to = aws_iam_role.bedrock_spend_cap
  id = "bedrock-spend-cap-lambda"
}

import {
  to = aws_iam_role_policy.bedrock_spend_cap
  id = "bedrock-spend-cap-lambda:bedrock-spend-cap"
}

import {
  to = aws_cloudwatch_log_group.bedrock_spend_cap
  id = "/aws/lambda/bedrock-spend-cap"
}

import {
  to = aws_lambda_function.bedrock_spend_cap
  id = "bedrock-spend-cap"
}

import {
  to = aws_cloudwatch_event_rule.bedrock_spend_cap_tick
  id = "bedrock-spend-cap-tick"
}

import {
  to = aws_cloudwatch_event_target.bedrock_spend_cap_tick
  id = "bedrock-spend-cap-tick/lambda"
}

import {
  to = aws_lambda_permission.bedrock_spend_cap_tick
  id = "bedrock-spend-cap/events-tick"
}
