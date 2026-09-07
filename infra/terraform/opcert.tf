# ---------------------------------------------------------------------------
# opcert.tf — trusted op-cert gate service edge (story 76d2, epic op-cert).
#
# The service uses the existing `rebar-gerrit` host and nginx TLS origin. New resources are
# pay-per-request API Gateway v2 with SigV4 and an injected origin guard, the sole
# `execute-api:Invoke` role, and two SSM SecureStrings covered by the existing instance grant.
# It needs no ECS/Fargate, load balancer/VPC link, Secrets Manager, customer CMK, or
# `kms:Sign`. The service performs SSHSIG signing.
#
# `data.aws_caller_identity.current` is declared in iam.tf; reused here.
# ---------------------------------------------------------------------------

variable "opcert_admin_principal_arns" {
  type        = list(string)
  description = <<-EOT
    IAM principal ARNs (the operator's admin IAM users/roles) allowed to assume
    `rebar-opcert-admin`, whose sole inline policy grants `execute-api:Invoke` on the
    op-cert API. Set at deploy, e.g. -var 'opcert_admin_principal_arns=["arn:aws:iam::<acct>:user/ops"]'.
    Empty by default so the role trusts nobody until the operator supplies principals.
  EOT
  default     = []
}

# --- Origin-guard shared secret -------------------------------------------
# Terraform stores this generated value in SSM and injects it as `X-Opcert-Guard`.
# Rotate with `terraform apply -replace=random_password.opcert_guard`, then run
# infra/scripts/materialize-opcert-guard.sh. `/opcert/` fails closed with 403 between steps.
resource "random_password" "opcert_guard" {
  length  = 48
  special = false # keep it header-safe (alnum) — it travels as an HTTP header value
}

# --- SSM SecureString parameters (under the EXISTING rebar-gerrit-ssm-params-read grant) ---

# The operator seeds the passphrase-free Ed25519 private key after apply. Write-only
# `value_wo` (ADR 0105) stays out of state and is resent only on a version change, so
# Terraform owns this slot's existence and type, not its value. The guard stays fully managed.
resource "aws_ssm_parameter" "opcert_ed25519_key" {
  name = "/rebar/prod/opcert-ed25519-key"
  type = "SecureString"
  # placeholder; operator seeds the real key out-of-band (see runbook). Write-only: never in state.
  value_wo         = "CHANGEME"
  value_wo_version = 1

  tags = {
    Project = "rebar"
  }
}

# The origin-guard value — FULLY Terraform-managed (its value IS random_password.result), so it
# carries NO `ignore_changes`: rotating the random_password updates this SSM value on apply.
resource "aws_ssm_parameter" "opcert_origin_guard" {
  name  = "/rebar/prod/opcert-origin-guard"
  type  = "SecureString"
  value = random_password.opcert_guard.result

  tags = {
    Project = "rebar"
  }
}

# --- API Gateway HTTP API (v2) — SigV4 front door -------------------------
# HTTP API (not REST API): pay-per-request, no fixed fee. HTTP API v2 has NO separate
# `aws_apigatewayv2_authorizer` for IAM — SigV4 is a ROUTE attribute (`authorization_type`).
resource "aws_apigatewayv2_api" "opcert" {
  name          = "rebar-opcert"
  protocol_type = "HTTP"
  description   = "Trusted op-cert gate service front door (SigV4-authenticated, proxies to the box nginx origin)."

  tags = {
    Project = "rebar"
  }
}

# HTTP_PROXY uses the HTTPS nginx origin because its HTTP redirect breaks integration. It
# forwards `{proxy}` and appends the guard. nginx refuses direct requests without a match.
resource "aws_apigatewayv2_integration" "opcert" {
  api_id                 = aws_apigatewayv2_api.opcert.id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  integration_uri        = "https://${var.dns_name}/opcert/{proxy}"
  payload_format_version = "1.0"

  request_parameters = {
    "append:header.X-Opcert-Guard" = random_password.opcert_guard.result
  }
}

# The route: ALL methods under /opcert/*, SigV4-authenticated (`AWS_IAM`).
resource "aws_apigatewayv2_route" "opcert" {
  api_id             = aws_apigatewayv2_api.opcert.id
  route_key          = "ANY /opcert/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.opcert.id}"
  authorization_type = "AWS_IAM"
}

# Default stage, auto-deployed (no manual deployment step).
resource "aws_apigatewayv2_stage" "opcert" {
  api_id      = aws_apigatewayv2_api.opcert.id
  name        = "$default"
  auto_deploy = true

  tags = {
    Project = "rebar"
  }
}

# --- IAM: the SOLE Invoke grantee -----------------------------------------
# Trust is limited to deploy-supplied principals. Route-level `AWS_IAM` admits only
# SigV4-signed requests from an Invoke-granted principal.
data "aws_iam_policy_document" "opcert_admin_assume" {
  statement {
    sid     = "AssumeOpcertAdmin"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = var.opcert_admin_principal_arns
    }
  }
}

resource "aws_iam_role" "opcert_admin" {
  name               = "rebar-opcert-admin"
  assume_role_policy = data.aws_iam_policy_document.opcert_admin_assume.json

  # The operator manages this trust list, whose default is empty. `ignore_changes` keeps the
  # scheduled drift plan from replacing it when the deploy variable is absent. Terraform owns
  # the role rather than its membership. See the deploy runbook.
  lifecycle {
    ignore_changes = [assume_role_policy]
  }

  tags = {
    Project = "rebar"
  }
}

# The ONLY policy in this IaC that grants execute-api:Invoke — scoped to EXACTLY this API's
# execution ARN (all stages/methods/paths under it). No other role/policy grants Invoke, so
# `rebar-opcert-admin` is the single principal that can call the API.
data "aws_iam_policy_document" "opcert_admin_invoke" {
  statement {
    sid       = "InvokeOpcertApi"
    actions   = ["execute-api:Invoke"]
    resources = ["${aws_apigatewayv2_api.opcert.execution_arn}/*"]
  }
}

resource "aws_iam_role_policy" "opcert_admin_invoke" {
  name   = "opcert_admin_invoke"
  role   = aws_iam_role.opcert_admin.id
  policy = data.aws_iam_policy_document.opcert_admin_invoke.json
}

# --- Outputs (recorded by the operator into the deploy-evidence comment) ----
output "opcert_api_id" {
  description = "HTTP API id of the op-cert gate front door."
  value       = aws_apigatewayv2_api.opcert.id
}

output "opcert_api_endpoint" {
  description = "Invoke URL of the op-cert gate API ($default stage). Callers SigV4-sign requests to <endpoint>/opcert/jobs."
  value       = aws_apigatewayv2_api.opcert.api_endpoint
}

output "opcert_admin_role_arn" {
  description = "ARN of the rebar-opcert-admin role — the sole execute-api:Invoke grantee."
  value       = aws_iam_role.opcert_admin.arn
}
