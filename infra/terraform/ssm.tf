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
    # GitHub OAuth App creds for the gerrit-oauth-provider plugin (b744/WS8).
    # Required once auth.type = OAUTH: client-id materialized into gerrit.config,
    # client-secret into secure.config. See infra/runbooks/gerrit-auth-hardening.md.
    "/rebar/prod/github-oauth-client-id",
    "/rebar/prod/github-oauth-client-secret",
    # CI Verified-vote gate (epic 1fa8 / story S4). Two secret slots for the
    # gerrit-to-platform → GitHub Actions → Gerrit `Verified` vote path (ADR-0022,
    # ADR-0023). NEITHER is consumed via the container .env (they are NOT in
    # fetch-secrets.sh / user_data.sh's curated map):
    #   - g2p-github-pat: the fine-grained, single-repo GitHub PAT that g2p uses to
    #     workflow_dispatch gerrit-verify.yaml. MATERIALISED at boot into
    #     gerrit_to_platform.ini (0600) by infra/gerrit/materialize-g2p-config.sh
    #     (fail-closed) — like the replication deploy key, never via env/ps.
    #   - ci-gerrit-ssh-key: the CI Gerrit service account's SSH PRIVATE key. The box
    #     never reads it; an operator copies its value into the GitHub Actions secret
    #     GERRIT_SSH_PRIVKEY so the workflow can SSH back into Gerrit :29418 to cast
    #     Verified. See infra/runbooks/g2p-ci-credentials.md for the operator steps.
    "/rebar/prod/g2p-github-pat",
    "/rebar/prod/ci-gerrit-ssh-key",
    # Code-review data capture (epic foliaged-merry-collie / story limestone-unethical-zebrafinch).
    # A fine-grained GitHub PAT with contents:write on the tickets repo ONLY — the reviewbot uses
    # it (via a URL-scoped git credential helper materialized from the container .env, see
    # fetch-secrets.sh's reviewbot-tickets-pat -> REVIEWBOT_TICKETS_PAT mapping) to push the
    # code_review artifact ticket events to origin/tickets. Operator populates this SecureString.
    "/rebar/prod/reviewbot-tickets-pat",
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
