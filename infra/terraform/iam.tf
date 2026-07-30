# ---------------------------------------------------------------------------
# IAM — instance role + DLM service role
# ---------------------------------------------------------------------------
# SINGLE-OWNER CONTRACT: this story (S1) OWNS the EC2 instance role
# `rebar-gerrit-instance-role`, its instance profile, and the DLM lifecycle
# role `rebar-dlm-lifecycle-role`. Downstream stories (S2, S4a) must NOT create
# another role — they ATTACH their own *separately-named, scoped* inline
# policies to this same role. Keeping one role with one owner avoids the
# "two configs fight over the same resource" drift class.
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}

# --- EC2 instance role ------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gerrit_instance" {
  name               = "rebar-gerrit-instance-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json

  tags = {
    Project = "rebar"
  }
}

resource "aws_iam_instance_profile" "gerrit_instance" {
  name = "rebar-gerrit-instance-profile"
  role = aws_iam_role.gerrit_instance.name
}

# SSM Session Manager — REQUIRED. This managed policy is the SOLE admin path
# into the box (no inbound SSH). Without it there is no way to get a shell.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.gerrit_instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Scoped SSM Parameter Store read — limited to the /rebar/prod/* namespace
# (NO wildcard on the whole account), plus kms:Decrypt for the AWS-managed SSM
# key constrained by the kms:ViaService condition (least privilege).
data "aws_iam_policy_document" "ssm_params_read" {
  statement {
    sid = "ReadRebarProdParams"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/rebar/prod/*",
    ]
  }

  statement {
    sid       = "DecryptSecureStringsViaSSM"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "ssm_params_read" {
  name   = "rebar-gerrit-ssm-params-read"
  role   = aws_iam_role.gerrit_instance.id
  policy = data.aws_iam_policy_document.ssm_params_read.json
}

# Minimal CloudWatch — push instance metrics + ship logs.
data "aws_iam_policy_document" "cloudwatch_basic" {
  statement {
    sid = "CloudWatchBasic"
    actions = [
      "cloudwatch:PutMetricData",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "cloudwatch_basic" {
  name   = "rebar-gerrit-cloudwatch-basic"
  role   = aws_iam_role.gerrit_instance.id
  policy = data.aws_iam_policy_document.cloudwatch_basic.json
}

# --- DLM (Data Lifecycle Manager) service role -----------------------------
# SINGLE-OWNER CONTRACT: S1 owns this role. backup.tf references its ARN as the
# DLM policy's execution_role_arn. S7 only MONITORS the resulting snapshots — it
# must not create a second DLM role or policy.

data "aws_iam_policy_document" "dlm_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dlm" {
  name               = "rebar-dlm-lifecycle-role"
  assume_role_policy = data.aws_iam_policy_document.dlm_assume.json

  tags = {
    Project = "rebar"
  }
}

resource "aws_iam_role_policy_attachment" "dlm_managed" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

# --- GitHub Actions OIDC role: terraform-plan drift detection (ticket c7d4) --
# SINGLE-OWNER: this ticket (c7d4) owns the drift-detection CI role. Assumed by
# .github/workflows/terraform-drift.yml via GitHub's OIDC provider to run READ-ONLY
# `terraform plan` on infra/terraform/**, so committed HCL that sits un-applied surfaces
# as a failing check (the root cause behind dc3f: manual applies drift silently).
#
# The OIDC provider (token.actions.githubusercontent.com) is owned by rebar (oidc.tf) —
# adopted from the decommissioned snap project (epic gaugeable-combatable-skylark) so rebar
# controls its own dependency rather than trusting a dead stack's ARN.
# Trust is scoped to THIS repo. Permissions = AWS-managed ReadOnlyAccess (covers S3 state
# read + ssm:GetParameter + every data-source read a plan performs) PLUS one inline
# statement for kms:Decrypt-via-SSM, which ReadOnlyAccess deliberately omits and the
# alert-endpoint SecureString data source (aws_ssm_parameter.alert_endpoint) needs. The CI
# runs `terraform plan -lock=false`, so NO state writes (and no S3 PutObject) are required.
data "aws_iam_policy_document" "gha_terraform_plan_assume" {
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
    # Scope to this repo (any ref/branch/PR). Narrow further with a specific ref if the
    # drift job is ever pinned to one branch.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:navapbc/rebar:*"]
    }
  }
}

resource "aws_iam_role" "gha_terraform_plan" {
  name               = "rebar-terraform-plan"
  assume_role_policy = data.aws_iam_policy_document.gha_terraform_plan_assume.json

  tags = {
    Project = "rebar"
    Ticket  = "c7d4"
  }
}

resource "aws_iam_role_policy_attachment" "gha_terraform_plan_readonly" {
  role       = aws_iam_role.gha_terraform_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# kms:Decrypt is NOT in ReadOnlyAccess; the plan reads the alert-endpoint SecureString
# (with_decryption = true) via SSM, so grant Decrypt constrained to the SSM service.
data "aws_iam_policy_document" "gha_terraform_plan_kms" {
  statement {
    sid       = "DecryptSecureStringsViaSSM"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "gha_terraform_plan_kms" {
  name   = "rebar-terraform-plan-kms"
  role   = aws_iam_role.gha_terraform_plan.id
  policy = data.aws_iam_policy_document.gha_terraform_plan_kms.json
}

# Bedrock — the review bot's LLM path (story S7 / ticket 9249). Scoped to ANTHROPIC Claude
# models only, so a call to any other vendor's model is denied (the least-privilege oracle).
#
# ACTION NAME: `bedrock:InvokeModel` — NOT `bedrock:Converse`. This is counter-intuitive and
# was got WRONG once, so the evidence is recorded here rather than left to memory.
#
# rebar reaches Bedrock through pydantic-ai's BedrockProvider, which calls the **Converse**
# API. `bedrock:Converse` does exist as a distinct, simulatable IAM action — which is why
# `aws iam simulate-custom-policy` reports implicitDeny for it under an InvokeModel-only
# grant, and why that simulation LOOKED like proof that Converse was the action to grant.
#
# It is not. The Converse API AUTHORIZES against `bedrock:InvokeModel`. Proven at runtime
# from the instance itself (SSM AWS-RunShellScript on i-00880b2c7f13527c5, running as
# assumed-role/rebar-gerrit-instance-role):
#
#   AccessDeniedException ... is not authorized to perform: bedrock:InvokeModel on resource:
#   arn:aws:bedrock:us-east-1:...:inference-profile/us.anthropic.claude-sonnet-4-6
#   because no identity-based policy allows the bedrock:InvokeModel action
#
# i.e. the policy SIMULATOR answers "what does the policy language permit for the action you
# named", NOT "which action does the service actually check". Only the from-instance call
# distinguishes them. Grant InvokeModel; re-verify from the instance, never from simulation
# alone and never from an operator identity holding bedrock:*.
#
# BOTH resource shapes are required for a cross-region inference profile: the profile ARN AND
# the underlying foundation models in every region the profile can route to. The region field
# is wildcarded; the MODEL is not.
data "aws_iam_policy_document" "bedrock_converse" {
  statement {
    sid = "ConverseAnthropicClaudeOnly"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*.anthropic.claude-*",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock_converse" {
  name   = "rebar-gerrit-bedrock-converse"
  role   = aws_iam_role.gerrit_instance.id
  policy = data.aws_iam_policy_document.bedrock_converse.json
}
