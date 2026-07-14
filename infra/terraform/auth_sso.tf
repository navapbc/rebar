# ---------------------------------------------------------------------------
# auth_sso.tf — domain-wide SSO foundation (re-homed from snap)
# ---------------------------------------------------------------------------
# Option B: a single central OAuth host (auth.solutions.navateam.com) that mints a
# domain-wide, HMAC-signed session cookie, plus per-subdomain edge gates that verify
# it. This file holds the SHARED locals + the cookie-signing HMAC key. The auth host
# itself (CloudFront + regional Lambda) is in auth_host.tf.
#
# Adopted from the decommissioned snap project (epic gaugeable-combatable-skylark).
# rebar keeps the stack alive so domain-wide SSO survives; physical resource names
# are PRESERVED (renaming would destroy+recreate live infra), so the legacy
# "snap-demo" name prefix is retained deliberately — see local.legacy_name_prefix.

locals {
  # PHYSICAL NAME PREFIX — DO NOT CHANGE. The adopted resources keep their original
  # snap-demo-* names; changing this value renames them, which for Lambda/API/role
  # forces DESTROY + RECREATE of 18 live, user-facing resources (new ARNs, new
  # CloudFront origin, SSO downtime). A future rename is a separate, deliberate
  # migration, not a tag/ownership change.
  legacy_name_prefix = "snap-demo"

  sso_domain         = "solutions.navateam.com"   # parent zone (var.dns_zone_id)
  auth_host_fqdn     = "auth.${local.sso_domain}" # the only host Google knows
  cookie_domain      = ".${local.sso_domain}"     # cookie scoped to every subdomain
  sso_hosted_domain  = "navapbc.com"              # required Google Workspace hd
  google_secret_name = "/auth-solutions/GOOGLE_CLIENT_SECRET"
  cookie_secret_name = "/auth-solutions/COOKIE_SIGNING_SECRET"
  google_client_id   = "114566571260-c3ia7544qqfnsrkvnnebf8dsbgj9gijn.apps.googleusercontent.com"
  session_ttl_hours  = 12 # bounded session lifetime
  auth_account_id    = data.aws_caller_identity.current.account_id
}

# Cookie-signing HMAC key. Managed at its EXISTING path with the same "existence +
# type, not value" contract rebar's other SSM secrets use (ssm.tf): the live value is
# preserved on import (ignore_changes), the auth-host Lambda reads it at RUNTIME, and
# terraform never needs to know the value. ROTATION (the SSO revoke lever): overwrite
# the value out-of-band (`aws ssm put-parameter --overwrite`) and redeploy the Lambda —
# see infra/runbooks/sso-auth-host.md. Rotating invalidates ALL live sessions.
resource "aws_ssm_parameter" "cookie_signing_secret" {
  name        = local.cookie_secret_name
  description = "HMAC key for the *.solutions.navateam.com SSO session cookie"
  type        = "SecureString"
  value       = "CHANGEME"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Project = "rebar"
  }
}

# The Google OAuth client_secret is provisioned out-of-band (SecureString). We never
# manage its VALUE — only reference its name so the Lambda policy can grant read.
data "aws_ssm_parameter" "google_client_secret" {
  name            = local.google_secret_name
  with_decryption = false # metadata/ARN only; value read at runtime by the Lambda
}
