# ---------------------------------------------------------------------------
# auth_host.tf — central SSO auth host (re-homed from snap)
# ---------------------------------------------------------------------------
# auth.solutions.navateam.com: a regional Lambda (us-east-1) fronted by CloudFront →
# API Gateway HTTP API. (Lambda Function URLs are blocked org-wide, hence API GW.)
# The public execute-api endpoint is gated by a shared origin secret that ONLY
# CloudFront injects (as a custom origin header) and the Lambda verifies — a direct
# hit lacking the header gets 403. The Lambda runs the Google OAuth code flow and
# mints the domain-wide session cookie. Adopted from snap (epic gaugeable-combatable-
# skylark); names preserved via local.legacy_name_prefix (see auth_sso.tf).

# Origin secret: embedded at apply time into BOTH the CloudFront custom origin header
# and the Lambda's ORIGIN_SECRET env var. Imported with its LIVE value so both sides
# stay in sync and nothing rotates. To rotate: `terraform apply
# -replace=random_password.auth_origin_secret` (updates the distribution + Lambda env
# in one run).
resource "random_password" "auth_origin_secret" {
  length  = 40
  special = false # header-safe: no chars that need quoting in an HTTP header value

  lifecycle {
    # This resource was IMPORTED (adopted from snap). `terraform import` cannot recover
    # a random_password's original generation args, so it records the provider default
    # `special = true` in state — which would otherwise force a REPLACEMENT (rotating the
    # live origin secret and briefly breaking the CloudFront↔Lambda handshake during
    # propagation). Ignoring the drift keeps the live value (`.result`) stable. A
    # deliberate rotation still uses the config's `special = false` via
    # `terraform apply -replace=random_password.auth_origin_secret`.
    ignore_changes = [special]
  }
}

# --- bundle: handler + shared cookie lib, zipped together --------------------
data "archive_file" "auth_host" {
  type        = "zip"
  output_path = "${path.module}/auth/dist/auth-host.zip"

  # Preserve the auth-host/ + lib/ layout inside the zip so index.js's
  # require("../lib/cookie") resolves identically to running it locally.
  source {
    content  = file("${path.module}/auth/auth-host/index.js")
    filename = "auth-host/index.js"
  }
  source {
    content  = file("${path.module}/auth/lib/cookie.js")
    filename = "lib/cookie.js"
  }
}

# --- IAM: logs + runtime read of the two SSO SSM secrets --------------------
data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm" # AWS-managed key that encrypts our SecureStrings
}

resource "aws_iam_role" "auth_host" {
  name = "${local.legacy_name_prefix}-auth-host"
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

resource "aws_iam_role_policy" "auth_host" {
  name = "auth-host"
  role = aws_iam_role.auth_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${local.auth_account_id}:log-group:/aws/lambda/${local.legacy_name_prefix}-auth-host:*"
      },
      {
        Sid    = "ReadSsoSecrets"
        Effect = "Allow"
        Action = ["ssm:GetParameters"]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${local.auth_account_id}:parameter${local.google_secret_name}",
          "arn:aws:ssm:${var.aws_region}:${local.auth_account_id}:parameter${local.cookie_secret_name}",
        ]
      },
      {
        Sid      = "DecryptSecureStrings"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = data.aws_kms_alias.ssm.target_key_arn
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com" }
        }
      },
    ]
  })
}

# --- the auth-host Lambda ----------------------------------------------------
resource "aws_lambda_function" "auth_host" {
  function_name    = "${local.legacy_name_prefix}-auth-host"
  role             = aws_iam_role.auth_host.arn
  runtime          = "nodejs20.x"
  handler          = "auth-host/index.handler"
  filename         = data.archive_file.auth_host.output_path
  source_code_hash = data.archive_file.auth_host.output_base64sha256
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      GOOGLE_CLIENT_ID    = local.google_client_id
      REDIRECT_URI        = "https://${local.auth_host_fqdn}/_callback"
      HOSTED_DOMAIN       = local.sso_hosted_domain
      COOKIE_DOMAIN       = local.cookie_domain
      BASE_DOMAIN         = local.sso_domain
      SESSION_TTL_SECONDS = tostring(local.session_ttl_hours * 3600)
      GOOGLE_SECRET_PARAM = local.google_secret_name
      COOKIE_SECRET_PARAM = local.cookie_secret_name
      ORIGIN_SECRET       = random_password.auth_origin_secret.result
    }
  }

  tags = {
    Project = "rebar"
  }
}

# --- API Gateway HTTP API in front of the Lambda ----------------------------
resource "aws_apigatewayv2_api" "auth_host" {
  name          = "${local.legacy_name_prefix}-auth-host"
  protocol_type = "HTTP"

  tags = {
    Project = "rebar"
  }
}

resource "aws_apigatewayv2_integration" "auth_host" {
  api_id                 = aws_apigatewayv2_api.auth_host.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.auth_host.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "auth_host" {
  api_id    = aws_apigatewayv2_api.auth_host.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.auth_host.id}"
}

resource "aws_apigatewayv2_stage" "auth_host" {
  api_id      = aws_apigatewayv2_api.auth_host.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "auth_host_apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auth_host.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.auth_host.execution_arn}/*/*"
}

# --- CloudFront in front of the HTTP API ------------------------------------
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}
data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "auth_host" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${local.legacy_name_prefix} central auth host (${local.auth_host_fqdn})"
  price_class     = "PriceClass_100"
  aliases         = [local.auth_host_fqdn]

  origin {
    domain_name = trimsuffix(trimprefix(aws_apigatewayv2_api.auth_host.api_endpoint, "https://"), "/")
    origin_id   = "auth-host-lambda"

    # Shared secret proving the request came through CloudFront. The execute-api
    # endpoint is itself public, so the Lambda 403s any request lacking this header;
    # CloudFront overrides a viewer-supplied header of the same name, so it can't be
    # spoofed.
    custom_header {
      name  = "X-Origin-Auth"
      value = random_password.auth_origin_secret.result
    }

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id         = "auth-host-lambda"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    compress                 = true
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate.wildcard.arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  # Shared, production-critical distribution; guard against accidental teardown.
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Project = "rebar"
  }
}

# --- DNS --------------------------------------------------------------------
resource "aws_route53_record" "auth_host_a" {
  zone_id = var.dns_zone_id
  name    = local.auth_host_fqdn
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.auth_host.domain_name
    zone_id                = aws_cloudfront_distribution.auth_host.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "auth_host_aaaa" {
  zone_id = var.dns_zone_id
  name    = local.auth_host_fqdn
  type    = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.auth_host.domain_name
    zone_id                = aws_cloudfront_distribution.auth_host.hosted_zone_id
    evaluate_target_health = false
  }
}
