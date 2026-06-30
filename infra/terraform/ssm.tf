# ---------------------------------------------------------------------------
# SSM SecureString parameters — secret slots under /rebar/prod/*
# ---------------------------------------------------------------------------
# These are created as PLACEHOLDERS with the value "CHANGEME". An operator MUST
# populate the real values (e.g. via `aws ssm put-parameter --overwrite`)
# BEFORE the S2 apply that brings up the instance. `user_data.sh` FAILS FAST if
# any fetched value is still the "CHANGEME" sentinel — cloud-init marks the
# instance failed rather than writing a broken config.
#
# `lifecycle { ignore_changes = [value] }` means once an operator overwrites a
# value out-of-band, terraform will NOT revert it back to "CHANGEME" on the
# next apply. Terraform owns the parameter's existence + type, not its value.
# ---------------------------------------------------------------------------

locals {
  # EXACT secret parameter names — keep in sync with user_data.sh and ADR 0012.
  rebar_secret_params = [
    "/rebar/prod/gerrit-admin-password",
    "/rebar/prod/gerrit-ssh-host-ed25519-key",
    "/rebar/prod/github-replication-deploy-key",
    "/rebar/prod/mcp-hmac-signing-key",
    "/rebar/prod/anthropic-api-key",
    "/rebar/prod/alert-endpoint",
    "/rebar/prod/gerrit-bot-token",
  ]
}

resource "aws_ssm_parameter" "rebar_secrets" {
  for_each = toset(local.rebar_secret_params)

  name  = each.value
  type  = "SecureString"
  value = "CHANGEME"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Project = "rebar"
  }
}
