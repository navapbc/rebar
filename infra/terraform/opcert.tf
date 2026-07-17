# ---------------------------------------------------------------------------
# opcert.tf — trusted op-cert gate service edge (story 76d2, epic op-cert).
#
# ZERO FIXED-COST posture (the epic's compute-runtime resolution): the gate service
# rides the EXISTING t4g.large `rebar-gerrit` box (a compose service behind the HOST
# nginx TLS origin — see infra/compose/docker-compose.yml + infra/nginx/rebar.conf.template).
# The only NEW cloud resources here are:
#   - an API Gateway HTTP API (v2) — PAY-PER-REQUEST, no fixed monthly fee — that
#     SigV4-authenticates callers (`authorization_type = "AWS_IAM"` on the route) and
#     proxies to the box's HTTPS nginx origin, injecting a static origin-guard header;
#   - an IAM role `rebar-opcert-admin` the API restricts Invoke to (trust from a deploy
#     variable), the SOLE grantee of `execute-api:Invoke`;
#   - two FREE SSM Parameter Store SecureString slots under /rebar/prod/* (already covered
#     by the instance profile's `rebar-gerrit-ssm-params-read` grant — NO new IAM here).
# NO Fargate/ECS, NO load balancer/VPC link, NO Secrets Manager, NO customer KMS CMK,
# NO kms:Sign (SSHSIG signing is done by the service itself).
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
# Terraform-generated (the hashicorp/random provider is already pinned in versions.tf).
# Its value is (a) stored as the /rebar/prod/opcert-origin-guard SSM SecureString below and
# (b) injected as the static `X-Opcert-Guard` request header on the API Gateway integration.
# Rotation = `terraform apply -replace=random_password.opcert_guard`, which updates BOTH the
# SSM value and the integration header together; the operator then re-runs
# infra/scripts/materialize-opcert-guard.sh to refresh the host-nginx map (brief fail-closed
# window between the two steps — /opcert/ serves 403 until the map is rewritten).
resource "random_password" "opcert_guard" {
  length  = 48
  special = false # keep it header-safe (alnum) — it travels as an HTTP header value
}

# --- SSM SecureString parameters (under the EXISTING rebar-gerrit-ssm-params-read grant) ---

# The environment's passphrase-free Ed25519 op-cert PRIVATE key. Declared as a placeholder;
# the operator SEEDS the real key out-of-band (`aws ssm put-parameter --overwrite`) after apply.
# `lifecycle { ignore_changes = [value] }` means a later `terraform apply` NEVER reverts/clobbers
# the operator-seeded key — Terraform owns the parameter's existence + type, not its value. This
# guard applies ONLY to the key parameter (the guard parameter below is fully Terraform-managed).
resource "aws_ssm_parameter" "opcert_ed25519_key" {
  name  = "/rebar/prod/opcert-ed25519-key"
  type  = "SecureString"
  value = "CHANGEME" # placeholder; operator seeds the real key out-of-band (see runbook)

  lifecycle {
    ignore_changes = [value]
  }

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

# HTTP_PROXY integration to the box's HTTPS nginx origin (NOT http://:80, which the nginx
# config 301-redirects and would break the integration). The greedy `{proxy}` path variable
# from the route is forwarded, and the static origin-guard header is APPENDED on every request
# (`append:header.X-Opcert-Guard`) — nginx rejects any /opcert/ request whose header does not
# match, so a direct-to-origin request that bypasses this API is refused.
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
# The admin role the API restricts Invoke to. Trust is limited to the deploy-supplied
# principal ARNs — no principal outside `opcert_admin_principal_arns` can assume it, and
# `AWS_IAM` route auth means only a SigV4-signed request from an Invoke-granted principal
# reaches the origin.
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

  # The trust principals are OPERATOR-SUPPLIED AT DEPLOY (`var.opcert_admin_principal_arns`,
  # empty by default) — the SAME operator-owned pattern as the key SSM param above, so it gets
  # the SAME guard. Without this, the terraform-drift check (`terraform plan` with no -var,
  # .github/workflows/terraform-drift.yml) renders an empty principal and reports a permanent
  # phantom "1 to change" against the live operator-applied principal, reddening the daily
  # drift sweep forever and masking real drift. Terraform owns the role's existence, not who
  # may assume it — the operator manages the trust list out-of-band (see the deploy runbook).
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
