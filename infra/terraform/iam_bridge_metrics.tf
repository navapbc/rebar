# Reconcile Bridge runs in GitHub Actions, outside the Gerrit host's telemetry
# probe. Its repo-scoped OIDC role writes bridge telemetry in rebar/host.
# CloudWatch PutMetricData requires resource "*". The namespace condition bounds
# the write. Keep this separate from read-only drift and Bedrock roles.

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
