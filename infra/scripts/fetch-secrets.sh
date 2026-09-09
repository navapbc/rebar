#!/usr/bin/env bash
# Materialize container secrets from SSM using the EC2 instance role (ADR 0008).
# Required reads finish before the atomic 0600 .env replacement; failures leave the
# prior file intact. Autodeploy treats this source as an MCP secrets deploy signal.
#
# SSM leaf -> container setting:
#   /rebar/prod/anthropic-api-key      -> ANTHROPIC_API_KEY     (review-bot LLM, S4b)
#   /rebar/prod/mcp-hmac-signing-key   -> MCP_HMAC_SIGNING_KEY  (legacy compatibility)
#   /rebar/prod/gerrit-admin-password  -> GERRIT_ADMIN_PASSWORD (admin bootstrap)
#   /rebar/prod/gerrit-bot-token       -> GERRIT_BOT_TOKEN      (bot posts reviews)
#   /rebar/prod/github-oauth-client-id     -> GITHUB_OAUTH_CLIENT_ID     (WS8, OPTIONAL)
#   /rebar/prod/github-oauth-client-secret -> GITHUB_OAUTH_CLIENT_SECRET (WS8, OPTIONAL)
#   /rebar/prod/reviewbot-tickets-pat      -> REVIEWBOT_TICKETS_PAT      (data capture, OPTIONAL)
#   /rebar/prod/mcp-tickets-pat            -> MCP_TICKETS_PAT            (MCP ticket store, OPTIONAL)
#   /rebar/prod/mcp-client-pat-copilot     -> MCP_CLIENT_PAT_COPILOT     (MCP static auth, OPTIONAL)
#   /rebar/prod/mcp-client-pat-codex       -> MCP_CLIENT_PAT_CODEX       (MCP static auth, OPTIONAL)
#   /rebar/prod/mcp-client-pat-claude      -> MCP_CLIENT_PAT_CLAUDE      (MCP static auth, OPTIONAL)
#   /rebar/prod/jira-url                   -> JIRA_URL                   (bridge, String,  OPTIONAL)
#   /rebar/prod/jira-user                  -> JIRA_USER                  (bridge, String,  OPTIONAL)
#   /rebar/prod/jira-project               -> JIRA_PROJECT               (bridge, String,  OPTIONAL)
#   /rebar/prod/jira-api-token             -> JIRA_API_TOKEN             (bridge, SECRET,  OPTIONAL)
# Jira's token is decrypted; its three configuration values remain plain Strings.
# Jira and OAuth values are optional here and enforced only by their consumers.
# REVIEW_BOT_PORT=8000 is generated locally; unrelated SSM leaves are owned elsewhere.
set -euo pipefail

# Output path (overridable for testing).
ENV_FILE="${ENV_FILE:-infra/compose/.env}"
SSM_PREFIX="/rebar/prod"

# Read the region through token-required IMDSv2.
imds_token="$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")"
AWS_REGION="$(curl -sf \
  -H "X-aws-ec2-metadata-token: ${imds_token}" \
  "http://169.254.169.254/latest/meta-data/placement/region")"
export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"

# Read a required, decrypted SecureString.
get_param() {
  local leaf="$1" val
  val="$(aws ssm get-parameter \
    --name "${SSM_PREFIX}/${leaf}" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text)"
  # Blank and placeholder values fail like missing parameters.
  if [ -z "${val}" ] || [ "${val}" = "None" ] || [ "${val}" = "CHANGEME" ]; then
    echo "fetch-secrets.sh: ${SSM_PREFIX}/${leaf} is empty/None/CHANGEME — aborting" >&2
    exit 1
  fi
  printf '%s' "${val}"
}

# Optional SecureStrings map missing, blank, None, or CHANGEME to an empty value.
get_param_optional() {
  local leaf="$1" val
  val="$(aws ssm get-parameter \
    --name "${SSM_PREFIX}/${leaf}" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || true)"
  if [ -z "${val}" ] || [ "${val}" = "None" ] || [ "${val}" = "CHANGEME" ]; then
    printf ''
    return 0
  fi
  printf '%s' "${val}"
}

# Optional plain Strings deliberately omit --with-decryption and use the same blank semantics.
get_param_optional_plain() {
  local leaf="$1" val
  val="$(aws ssm get-parameter \
    --name "${SSM_PREFIX}/${leaf}" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || true)"
  if [ -z "${val}" ] || [ "${val}" = "None" ] || [ "${val}" = "CHANGEME" ]; then
    printf ''
    return 0
  fi
  printf '%s' "${val}"
}

# Resolve required values before creating any replacement output.
anthropic_api_key="$(get_param anthropic-api-key)"
mcp_hmac_signing_key="$(get_param mcp-hmac-signing-key)"
gerrit_admin_password="$(get_param gerrit-admin-password)"
gerrit_bot_token="$(get_param gerrit-bot-token)"
# OAuth is enforced downstream only when auth.type is OAUTH.
github_oauth_client_id="$(get_param_optional github-oauth-client-id)"
github_oauth_client_secret="$(get_param_optional github-oauth-client-secret)"
# The review-bot tickets PAT is optional; blank defers artifact pushes.
reviewbot_tickets_pat="$(get_param_optional reviewbot-tickets-pat)"
# The MCP tickets PAT is optional; blank defers its URL-scoped store clone.
mcp_tickets_pat="$(get_param_optional mcp-tickets-pat)"
# Prefer the MCP-specific PAT, but reuse the review-bot PAT for the same repo/branch.
# This data-store fallback never applies to the fail-closed static-auth token set.
if [ -z "${mcp_tickets_pat}" ] && [ -n "${reviewbot_tickets_pat}" ]; then
  mcp_tickets_pat="${reviewbot_tickets_pat}"
  echo "fetch-secrets.sh: mcp-tickets-pat is blank — falling back to reviewbot-tickets-pat for the MCP ticket store (same repo/branch); set the dedicated slot to scope it separately" >&2
fi

# Materialize the optional multiline authorship key as a 0600 file. Each service
# supplies its own container path, so REBAR_IDENTITY_SIGNING_KEY is not duplicated in .env.
rebar_bot_signing_key="$(get_param_optional rebar-bot-signing-key)"
# Always create the bind source; an empty file is the supported unsigned state.
signing_key_path="$(dirname "${ENV_FILE}")/rebar-bot-signing-key"
key_tmp="$(mktemp "${signing_key_path}.XXXXXX")"
chmod 600 "${key_tmp}"
printf '%s' "${rebar_bot_signing_key}" > "${key_tmp}"
[ -n "${rebar_bot_signing_key}" ] && printf '\n' >> "${key_tmp}"
mv -f "${key_tmp}" "${signing_key_path}"
chmod 600 "${signing_key_path}"
if [ -n "${rebar_bot_signing_key}" ]; then
  echo "fetch-secrets.sh: materialized rebar-bot signing key to ${signing_key_path} (0600)" >&2
else
  echo "fetch-secrets.sh: rebar-bot-signing-key is blank — wrote an EMPTY ${signing_key_path};" \
       "the review bot will write UNSIGNED events" >&2
fi

# Materialize the current Ed25519 op-cert verdict-signing key as a 0600 file outside
# the application. Always create the bind source; an empty file fails startup explicitly.
opcert_signing_key="$(get_param_optional opcert-ed25519-key)"
opcert_key_path="$(dirname "${ENV_FILE}")/opcert-ed25519-key"
opcert_key_tmp="$(mktemp "${opcert_key_path}.XXXXXX")"
chmod 600 "${opcert_key_tmp}"
printf '%s' "${opcert_signing_key}" > "${opcert_key_tmp}"
[ -n "${opcert_signing_key}" ] && printf '\n' >> "${opcert_key_tmp}"
mv -f "${opcert_key_tmp}" "${opcert_key_path}"
chmod 600 "${opcert_key_path}"
if [ -n "${opcert_signing_key}" ]; then
  echo "fetch-secrets.sh: materialized op-cert signing key to ${opcert_key_path} (0600)" >&2
else
  # gitleaks:allow — "opcert-ed25519-key" here is the public SSM leaf NAME (an identifier), not
  # key material; the generic-api-key rule trips on the "ed25519…key" adjacency. No secret is echoed.
  echo "fetch-secrets.sh: opcert-ed25519-key is blank — wrote an EMPTY ${opcert_key_path}; the op-cert gate service will fail startup key composition until the SSM slot is set" >&2  # gitleaks:allow
fi

# Keep raw per-client PATs only in the 0600 .env; the always-present token file names
# their environment variables. Blank records are omitted, so an all-blank set fails closed.
mcp_pat_copilot="$(get_param_optional mcp-client-pat-copilot)"
mcp_pat_codex="$(get_param_optional mcp-client-pat-codex)"
mcp_pat_claude="$(get_param_optional mcp-client-pat-claude)"

# Jira is an outbound integration, so missing optional values disable only bridge tools.
# Static MCP authentication remains independently fail-closed. Only Jira's token is decrypted.
jira_url="$(get_param_optional_plain jira-url)"
jira_user="$(get_param_optional_plain jira-user)"
# Live bridge checks also require JIRA_PROJECT from the environment.
jira_project="$(get_param_optional_plain jira-project)"
jira_api_token="$(get_param_optional jira-api-token)"
mcp_static_tokens_path="$(dirname "${ENV_FILE}")/mcp-static-tokens.json"

# Add token_env records only for populated clients; never interpolate their secrets.
mcp_records=""
add_mcp_record() {
  local client="$1" value="$2" envvar="$3"
  [ -z "${value}" ] && return 0
  [ -n "${mcp_records}" ] && mcp_records="${mcp_records}, "
  mcp_records="${mcp_records}{\"name\": \"${client}\", \"client_id\": \"${client}\", \"scopes\": [], \"token_env\": \"${envvar}\"}"
}
add_mcp_record copilot "${mcp_pat_copilot}" MCP_CLIENT_PAT_COPILOT
add_mcp_record codex "${mcp_pat_codex}" MCP_CLIENT_PAT_CODEX
add_mcp_record claude "${mcp_pat_claude}" MCP_CLIENT_PAT_CLAUDE

mcp_tokens_tmp="$(mktemp "${mcp_static_tokens_path}.XXXXXX")"
chmod 600 "${mcp_tokens_tmp}"
printf '{"tokens": [%s]}\n' "${mcp_records}" > "${mcp_tokens_tmp}"
mv -f "${mcp_tokens_tmp}" "${mcp_static_tokens_path}"
chmod 600 "${mcp_static_tokens_path}"
if [ -n "${mcp_records}" ]; then
  echo "fetch-secrets.sh: wrote ${mcp_static_tokens_path} (0600) with MCP static-token records" >&2
else
  echo "fetch-secrets.sh: no MCP client PATs set — wrote an EMPTY token set to ${mcp_static_tokens_path}; the static verifier fails-closed until ≥1 PAT is populated" >&2
fi

# Atomically replace the generated 0600 environment file.
tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
chmod 600 "${tmp}"
{
  echo "# GENERATED by fetch-secrets.sh from SSM ${SSM_PREFIX}/* — DO NOT COMMIT/EDIT."
  echo "# Regenerated each boot; this file is git-ignored and 0600."
  echo "ANTHROPIC_API_KEY=${anthropic_api_key}"
  echo "MCP_HMAC_SIGNING_KEY=${mcp_hmac_signing_key}"
  echo "GERRIT_ADMIN_PASSWORD=${gerrit_admin_password}"
  echo "GERRIT_BOT_TOKEN=${gerrit_bot_token}"
  echo "GITHUB_OAUTH_CLIENT_ID=${github_oauth_client_id}"
  echo "GITHUB_OAUTH_CLIENT_SECRET=${github_oauth_client_secret}"
  echo "REVIEWBOT_TICKETS_PAT=${reviewbot_tickets_pat}"
  echo "MCP_TICKETS_PAT=${mcp_tickets_pat}"
  # Each service sets its own REBAR_IDENTITY_SIGNING_KEY path; do not duplicate it here.
  # The token file references these PAT variable names and never contains their values.
  echo "MCP_CLIENT_PAT_COPILOT=${mcp_pat_copilot}"
  echo "MCP_CLIENT_PAT_CODEX=${mcp_pat_codex}"
  echo "MCP_CLIENT_PAT_CLAUDE=${mcp_pat_claude}"
  # Blank Jira values keep the container up while bridge tools report unavailable.
  echo "JIRA_URL=${jira_url}"
  echo "JIRA_USER=${jira_user}"
  echo "JIRA_PROJECT=${jira_project}"
  echo "JIRA_API_TOKEN=${jira_api_token}"
  echo "REVIEW_BOT_PORT=8000"
} >"${tmp}"
mv -f "${tmp}" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

echo "fetch-secrets.sh: wrote ${ENV_FILE} (0600) from ${SSM_PREFIX}/* in ${AWS_REGION}" >&2
