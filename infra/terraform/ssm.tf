# ---------------------------------------------------------------------------
# SSM SecureString parameters — secret slots under /rebar/prod/*
# ---------------------------------------------------------------------------
# Required `CHANGEME` slots must be seeded before the S2 instance apply. user_data.sh makes
# those placeholders boot-fatal. Optional MCP/Jira slots below degrade without aborting boot.
#
# `value_wo` keeps secrets out of Terraform state. The provider sends it only on create or a
# `value_wo_version` bump. Terraform owns each slot's existence and type, not its value.
# Seed and rotate per infra/runbooks/ssm-secret-write-only.md (ADR 0105).
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
    # Verified-vote credentials bypass the container environment under ADRs 0022 and 0023. The g2p
    # PAT is materialized fail-closed into 0600 gerrit_to_platform.ini for workflow dispatch.
    # The box never reads the CI SSH key. The operator copies it to GitHub's
    # GERRIT_SSH_PRIVKEY so CI can vote over Gerrit SSH. See infra/runbooks/g2p-ci-credentials.md.
    "/rebar/prod/g2p-github-pat",
    "/rebar/prod/ci-gerrit-ssh-key",
    # A tickets-only contents:write PAT lets reviewbot push code_review events through its
    # URL-scoped credential helper (REVIEWBOT_TICKETS_PAT). The operator supplies it.
    "/rebar/prod/reviewbot-tickets-pat",
    # The operator-supplied Rebar Bot Ed25519 key becomes a 0600 identity.signing_key file for
    # review-bot/auto-lander, not an environment value. GitHub stores the same key as
    # REBAR_BOT_SIGNING_KEY for reconcile-bridge and canary workflows.
    "/rebar/prod/rebar-bot-signing-key",
    # Optional per-client PATs authenticate the nginx `/mcp/` edge through the static verifier.
    # Blank slots are omitted. Nonblank values become MCP_CLIENT_PAT_* entries in a 0600,
    # rsync-excluded environment file referenced by mcp-static-tokens.json. Rotation requires
    # re-materialization and a rebar-mcp restart. See infra/runbooks/mcp-client-pats.md.
    "/rebar/prod/mcp-client-pat-copilot",
    "/rebar/prod/mcp-client-pat-codex",
    "/rebar/prod/mcp-client-pat-claude",
    # The optional tickets-only contents:write PAT lets MCP's URL-scoped helper
    # (MCP_TICKETS_PAT) clone `tickets` into REBAR_TRACKER_DIR and push events. A blank slot
    # defers the clone without failing the container. The operator supplies the value.
    "/rebar/prod/mcp-tickets-pat",
    # JIRA_API_TOKEN is the bridge's only secret. URL, user, and project remain non-secret
    # workflow variables. It is optional at the container boundary. A blank token makes bridge
    # tools unavailable without aborting boot.
    "/rebar/prod/jira-api-token",
  ]
}

resource "aws_ssm_parameter" "rebar_secrets" {
  for_each = toset(local.rebar_secret_params)

  name = each.value
  type = "SecureString"
  # State stores only the version for write-only values. The operator seeds values out of band.
  # Bump value_wo_version for a Terraform-driven rotation under ADR 0105. See the runbook.
  value_wo         = "CHANGEME"
  value_wo_version = 1

  tags = {
    Project = "rebar"
  }
}

# ---------------------------------------------------------------------------
# SSM String parameters — NON-SECRET config slots under /rebar/prod/*
# ---------------------------------------------------------------------------
# Plaintext Jira URL, user, and project mirror workflow variables. Only the API token above is
# secret, so operators can inspect these values independently of token rotation.
#
# Operator-seeded jira-url/jira-user use imports plus `ignore_changes = [value]`. Terraform
# owns their existence and type, not their values. SecureStrings instead use write-only values.
#
# jira-project is fully Terraform-managed. The access probe reads it only from the environment
# and fails closed when missing. A test pins it to rebar.toml's project key.
# ---------------------------------------------------------------------------

locals {
  # Operator-seeded, value-preserving plain params (adopted by the import blocks below).
  rebar_plain_seeded_params = [
    "/rebar/prod/jira-url",
    "/rebar/prod/jira-user",
  ]
}

resource "aws_ssm_parameter" "rebar_plain_seeded" {
  for_each = toset(local.rebar_plain_seeded_params)

  name  = each.value
  type  = "String"
  value = "CHANGEME"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Project = "rebar"
  }
}

# Fully terraform-managed: the Jira project key. Single-sourced with rebar.toml's
# `[jira] project`; a test pins the two equal.
resource "aws_ssm_parameter" "jira_project" {
  name  = "/rebar/prod/jira-project"
  type  = "String"
  value = "REB"

  tags = {
    Project = "rebar"
  }
}

# --- Adopt the out-of-band-seeded Jira parameters into state ----------------
# These idempotent imports adopt three operator-created parameters instead of failing with
# `ParameterAlreadyExists`. They contain names only and may be removed after adoption.
import {
  to = aws_ssm_parameter.rebar_plain_seeded["/rebar/prod/jira-url"]
  id = "/rebar/prod/jira-url"
}

import {
  to = aws_ssm_parameter.rebar_plain_seeded["/rebar/prod/jira-user"]
  id = "/rebar/prod/jira-user"
}

import {
  to = aws_ssm_parameter.rebar_secrets["/rebar/prod/jira-api-token"]
  id = "/rebar/prod/jira-api-token"
}

# --- Adopt the out-of-band-seeded per-client MCP bearer PATs into state -------
# These imports adopt the three operator-seeded MCP PATs without reading their values into
# state. Write-only adoption records only existence, type, and version under ADR 0105.
import {
  to = aws_ssm_parameter.rebar_secrets["/rebar/prod/mcp-client-pat-copilot"]
  id = "/rebar/prod/mcp-client-pat-copilot"
}

import {
  to = aws_ssm_parameter.rebar_secrets["/rebar/prod/mcp-client-pat-codex"]
  id = "/rebar/prod/mcp-client-pat-codex"
}

import {
  to = aws_ssm_parameter.rebar_secrets["/rebar/prod/mcp-client-pat-claude"]
  id = "/rebar/prod/mcp-client-pat-claude"
}
