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
    # Authenticated-authorship signing key (epic cummy-monkeyish-dassie / story 297d, task bffe).
    # The Rebar Bot's ed25519 PRIVATE key. Materialized at boot to a 0600 file for the AWS
    # review-bot + auto-lander containers (identity.signing_key), following the ci-gerrit-ssh-key /
    # g2p-github-pat materialize-to-file precedent (NOT the container .env, which holds only
    # single-line tokens). The same private key is also stored as the GitHub Actions secret
    # REBAR_BOT_SIGNING_KEY for the reconcile-bridge + canary workflows. Operator populates the value.
    "/rebar/prod/rebar-bot-signing-key",
    # Per-client MCP bearer PATs (epic jira-reb-3527 "Enable MCP on AWS" / ADR 0104 §1). One
    # SecureString per client (copilot/codex/claude): the bearer token each client presents to
    # the nginx `/mcp/` TLS edge, which the `static` verifier authenticates. OPTIONAL at the
    # container boundary — fetch-secrets.sh reads them via get_param_optional and OMITS a blank
    # slot from the materialized tokens file (so the verifier never fails on an empty token_env).
    # Materialized on-box each boot (the .env is rsync-EXCLUDED, gotcha f600): the raw value lands
    # in the 0600 .env as MCP_CLIENT_PAT_*, and mcp-static-tokens.json references it via token_env.
    # Rotation is operator-driven (re-materialize + restart rebar-mcp; a value-only rotation does
    # NOT advance main so autodeploy no-ops) — see infra/runbooks/mcp-client-pats.md.
    "/rebar/prod/mcp-client-pat-copilot",
    "/rebar/prod/mcp-client-pat-codex",
    "/rebar/prod/mcp-client-pat-claude",
    # The MCP server's ticket-store PAT. A fine-grained GitHub PAT with contents:write on the
    # tickets repo ONLY — the mcp container's entrypoint uses it (via a URL-scoped git credential
    # helper materialized from the container .env, see fetch-secrets.sh's mcp-tickets-pat ->
    # MCP_TICKETS_PAT mapping) to clone the `tickets` branch into REBAR_TRACKER_DIR and push the
    # ticket events its tools write. OPTIONAL at the container boundary: fetch-secrets.sh reads it
    # via get_param_optional and a blank slot only DEFERS the clone (the container still boots).
    # Operator populates this SecureString.
    "/rebar/prod/mcp-tickets-pat",
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
