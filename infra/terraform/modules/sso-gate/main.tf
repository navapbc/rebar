# sso-gate: a published viewer-request Lambda@Edge that enforces the shared
# Option-B session cookie for one CloudFront distribution. See README.md.
#
# The provider passed in MUST be us-east-1 (a Lambda@Edge requirement); the
# caller's default provider in infra/shared already is.

terraform {
  required_providers {
    aws     = { source = "hashicorp/aws" }
    archive = { source = "hashicorp/archive" }
  }
}

# --- bundle: handler + shared cookie lib + baked config ----------------------
data "archive_file" "gate" {
  type        = "zip"
  output_path = "${var.source_root}/dist/edge-gate-${var.name}.zip"

  source {
    content  = file("${var.source_root}/edge-gate/index.js")
    filename = "edge-gate/index.js"
  }
  source {
    content  = file("${var.source_root}/lib/cookie.js")
    filename = "lib/cookie.js"
  }
  source {
    content = templatefile("${var.source_root}/edge-gate/config.js.tftpl", {
      signing_secret = var.signing_secret
      auth_host_url  = var.auth_host_url
      cookie_name    = var.cookie_name
    })
    filename = "edge-gate/config.js"
  }
}

# --- IAM: Lambda@Edge execution role ----------------------------------------
resource "aws_iam_role" "gate" {
  name = "${var.name}-sso-gate"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["lambda.amazonaws.com", "edgelambda.amazonaws.com"] }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Lambda@Edge logs land in the region nearest the viewer, so the ARN can't be
# pinned to a single region.
resource "aws_iam_role_policy" "gate_logs" {
  name = "edge-logs"
  role = aws_iam_role.gate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "arn:aws:logs:*:${var.account_id}:log-group:/aws/lambda/*.${var.name}-sso-gate:*"
    }]
  })
}

# --- the Lambda@Edge function (published + us-east-1) ------------------------
resource "aws_lambda_function" "gate" {
  function_name    = "${var.name}-sso-gate"
  role             = aws_iam_role.gate.arn
  runtime          = "nodejs20.x"
  handler          = "edge-gate/index.handler"
  filename         = data.archive_file.gate.output_path
  source_code_hash = data.archive_file.gate.output_base64sha256
  publish          = true # Lambda@Edge associates a specific version, not $LATEST
  timeout          = 5    # viewer-request hard cap
  memory_size      = 128
  # No environment{} — Lambda@Edge forbids env vars (config is baked).
}
