# `sso-gate` — reusable viewer-request SSO gate for a CloudFront distribution

Drops an Option-B SSO gate in front of any `*.solutions.navateam.com` CloudFront
distribution: a viewer-request Lambda@Edge that requires a valid shared session
cookie and otherwise 302s to the central auth host. The module builds and
publishes the Lambda; **the consumer associates the output ARN** on its own
distribution's cache behavior (Terraform can't inject an association into a
distribution defined elsewhere).

## Opt-in shape (two blocks)

```hcl
module "my_sso_gate" {
  source         = "../modules/sso-gate"
  name           = "${var.name}-mydash"            # → Lambda "<name>-sso-gate"
  account_id     = local.account_id
  source_root    = "${path.module}/auth"           # dir holding edge-gate/ + lib/
  signing_secret = data.aws_ssm_parameter.cookie_signing_secret.value
  auth_host_url  = "https://auth.${var.domain}"
}

# ...then inside the distribution you want to protect:
default_cache_behavior {
  # ...existing settings...
  lambda_function_association {
    event_type   = "viewer-request"
    lambda_arn   = module.my_sso_gate.qualified_arn
    include_body = false
  }
}
```

That's the entire per-subdomain cost — **no new Google client, no new redirect
URI**. The central auth host already covers every subdomain because the session
cookie is scoped to `.solutions.navateam.com`.

## Inputs

| Name | Description |
|---|---|
| `name` | Resource prefix; the Lambda/role are `<name>-sso-gate`. |
| `account_id` | AWS account id (for the log-group ARN scope). |
| `source_root` | Path to the auth source dir containing `edge-gate/` and `lib/cookie.js` (single-sourced with the auth host). |
| `signing_secret` | The cookie-signing key (baked into the bundle — Lambda@Edge can't read SSM at viewer-request). Pass `data.aws_ssm_parameter.cookie_signing_secret.value`. |
| `auth_host_url` | `https://auth.<domain>` — where unauthenticated viewers are sent. |
| `cookie_name` | Session cookie name (default `__Secure-sso`). |

## Outputs

| Name | Description |
|---|---|
| `qualified_arn` | Published Lambda version ARN — associate this on the distribution. |
| `function_name` | The function name. |
| `version` | The published version number. |

## Notes

- The Lambda is **published** (`publish = true`) and lives in **us-east-1** —
  both Lambda@Edge requirements.
- Rotating the signing secret re-bakes and republishes every gate built from
  this module — see `infra/shared/auth/RUNBOOK.md`.
