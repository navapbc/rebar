# ---------------------------------------------------------------------------
# iam_bridge_metrics.tf — GitHub Actions OIDC role: Reconcile Bridge telemetry
# (ticket 58e0-ca58-c5ab-4322)
# ---------------------------------------------------------------------------
# The Reconcile Bridge runs on GitHub Actions, not on the Gerrit host, so the host probe
# (infra/scripts/observability.sh) that publishes every other rebar/host metric structurally
# cannot observe it. The 2026-08-12 JIRA_API_TOKEN expiry proved the gap: hours of bridge
# failure with ZERO CloudWatch signal. .github/workflows/reconcile-bridge.yml now publishes
# rebar/host:bridge_run_failures itself, and needs an identity to do it.
#
# A SEPARATE ROLE, not a widened existing one. Two roles could plausibly have been reused:
#
#   - rebar-terraform-plan (iam.tf) — the drift-detection role. It carries AWS-managed
#     ReadOnlyAccess, and its ticket declares it SINGLE-OWNER. Adding a write action to a role
#     whose entire security story is "read-only" is exactly the kind of quiet scope creep that
#     makes a least-privilege claim untrue later.
#   - the Bedrock CI role — scoped to bedrock:InvokeModel for the live-model arms. Unrelated
#     workload, unrelated blast radius.
#
# Widening either would give the WHOLE of that role's usage the new permission. A distinct role
# keeps the bridge's write capability to the bridge.
#
# WHAT MAKES IT NARROW: cloudwatch:PutMetricData takes no resource ARN — `resources` must be
# ["*"], so a bare grant lets the holder write to ANY namespace, including the namespaces the
# production alarms read (falsifying rebar-autodeploy-* or rebar-gerrit-* at will). The
# cloudwatch:namespace condition key is what actually bounds it, pinning writes to rebar/host.
# The action list is a single action: no Get/List/DescribeAlarms, no SetAlarmState.
data "aws_iam_policy_document" "gha_bridge_metrics_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Scoped to THIS repo, matching the rebar-terraform-plan trust policy. The bridge runs on
    # both the schedule and workflow_dispatch off whatever ref is current, so pinning a single
    # ref here would break the scheduled pass.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:navapbc/rebar:*"]
    }
  }
}

resource "aws_iam_role" "gha_bridge_metrics" {
  name               = "rebar-bridge-metrics"
  description        = "GitHub Actions OIDC role: publish rebar/host bridge telemetry to CloudWatch"
  assume_role_policy = data.aws_iam_policy_document.gha_bridge_metrics_assume.json

  tags = {
    Project = "rebar"
    Ticket  = "58e0"
  }
}

data "aws_iam_policy_document" "gha_bridge_metrics_put" {
  statement {
    sid     = "PutMetricDataToRebarHostNamespaceOnly"
    actions = ["cloudwatch:PutMetricData"]
    # Not a wildcard by choice: PutMetricData is not resource-scopable in IAM.
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["rebar/host"]
    }
  }
}

resource "aws_iam_role_policy" "gha_bridge_metrics_put" {
  name   = "rebar-bridge-metrics-put"
  role   = aws_iam_role.gha_bridge_metrics.id
  policy = data.aws_iam_policy_document.gha_bridge_metrics_put.json
}

# The workflow reads the ARN from a repo Variable (vars.AWS_BRIDGE_METRICS_ROLE_ARN) rather than
# hardcoding it, and skips the metric steps entirely when it is unset — so this output is the
# value an operator copies into that Variable after `terraform apply`. Until then the workflow
# behaves exactly as it did before.
output "bridge_metrics_role_arn" {
  description = "Set as repo Variable AWS_BRIDGE_METRICS_ROLE_ARN for reconcile-bridge.yml"
  value       = aws_iam_role.gha_bridge_metrics.arn
}
