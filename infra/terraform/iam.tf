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
