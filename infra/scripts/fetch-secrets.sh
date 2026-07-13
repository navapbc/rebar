#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# fetch-secrets.sh — write the container .env from SSM Parameter Store (ADR-0008).
#
# Reads the SUBSET of /rebar/prod/* SecureString params the containers need and
# writes them to infra/compose/.env (0600), authenticating via the EC2 INSTANCE
# ROLE (no static keys). Idempotent: overwrites the .env each run. FAIL-FAST: if
# any SSM read fails (SSM unreachable / param missing), abort with exit 1 and do
# NOT touch the .env — never run on a stale secrets file.
#
# SSM-leaf -> env-var mapping (only the leaves the containers consume):
#   /rebar/prod/anthropic-api-key      -> ANTHROPIC_API_KEY     (review-bot LLM, S4b)
#   /rebar/prod/mcp-hmac-signing-key   -> MCP_HMAC_SIGNING_KEY  (verdict signing)
#   /rebar/prod/gerrit-admin-password  -> GERRIT_ADMIN_PASSWORD (admin bootstrap)
#   /rebar/prod/gerrit-bot-token       -> GERRIT_BOT_TOKEN      (bot posts reviews)
#   /rebar/prod/github-oauth-client-id     -> GITHUB_OAUTH_CLIENT_ID     (WS8, OPTIONAL)
#   /rebar/prod/github-oauth-client-secret -> GITHUB_OAUTH_CLIENT_SECRET (WS8, OPTIONAL)
#   /rebar/prod/reviewbot-tickets-pat      -> REVIEWBOT_TICKETS_PAT      (data capture, OPTIONAL)
#   /rebar/prod/autolander-gerrit-token    -> AUTOLANDER_GERRIT_TOKEN    (auto-lander, REQUIRED/fail-fast)
# The two OAuth creds are OPTIONAL here (blank if unpopulated) — they are only needed
# under auth.type = OAUTH, and compose-up.sh FAILS LOUD if OAUTH is selected but they
# are empty. Making them REQUIRED here would couple every boot (incl. non-OAUTH rollback)
# to their presence.
# Plus non-secrets: REVIEW_BOT_PORT=8000 and AUTOLANDER_PORT=8081 (8080 is Gerrit's; single-source the
# ports for compose + nginx).
# (The other /rebar/prod/* params — ssh host key, replication deploy key, alert
# endpoint — are consumed elsewhere, not by these containers, so they are not fetched.)
# ---------------------------------------------------------------------------
set -euo pipefail

# Output path (overridable for testing).
ENV_FILE="${ENV_FILE:-infra/compose/.env}"
SSM_PREFIX="/rebar/prod"

# --- Region via IMDSv2 (token-required) ------------------------------------
# IMDSv2 is enforced on the box, so fetch a session token before reading metadata.
imds_token="$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")"
AWS_REGION="$(curl -sf \
  -H "X-aws-ec2-metadata-token: ${imds_token}" \
  "http://169.254.169.254/latest/meta-data/placement/region")"
export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"

# --- Read one SecureString param (decrypted), fail-fast --------------------
# Echoes the decrypted value; aborts the whole script if the read fails.
get_param() {
  local leaf="$1" val
  val="$(aws ssm get-parameter \
    --name "${SSM_PREFIX}/${leaf}" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text)"
  # Harden the fail-fast: a successful call that yields an empty value or the
  # literal "None" (or the unpopulated placeholder) must NOT silently produce a
  # broken `KEY=` line — abort instead.
  if [ -z "${val}" ] || [ "${val}" = "None" ] || [ "${val}" = "CHANGEME" ]; then
    echo "fetch-secrets.sh: ${SSM_PREFIX}/${leaf} is empty/None/CHANGEME — aborting" >&2
    exit 1
  fi
  printf '%s' "${val}"
}

# --- Read one OPTIONAL SecureString param ----------------------------------
# Like get_param but NEVER aborts: yields empty if the param is absent, empty,
# "None", or the "CHANGEME" placeholder. Used for conditionally-required creds
# whose presence is enforced downstream (compose-up, only under auth.type = OAUTH).
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

# Fetch all required params FIRST (into shell vars) so a failure aborts BEFORE we
# overwrite the existing .env — a partial/empty .env must never be left behind.
anthropic_api_key="$(get_param anthropic-api-key)"
mcp_hmac_signing_key="$(get_param mcp-hmac-signing-key)"
gerrit_admin_password="$(get_param gerrit-admin-password)"
gerrit_bot_token="$(get_param gerrit-bot-token)"
# OPTIONAL (blank until an operator populates them + auth.type = OAUTH is in use).
github_oauth_client_id="$(get_param_optional github-oauth-client-id)"
github_oauth_client_secret="$(get_param_optional github-oauth-client-secret)"
# OPTIONAL: the reviewbot's tickets-repo PAT (contents:write on the tickets repo only). Blank
# until the operator populates the SSM slot; the container boots either way, and the code_review
# artifact push (story limestone-unethical-zebrafinch) starts working once it is set.
reviewbot_tickets_pat="$(get_param_optional reviewbot-tickets-pat)"
# REQUIRED (fail-fast): the auto-lander's Gerrit HTTP password (epic f1fa / S5). The lander's
# SOLE job is landing changes, so a blank token = a silent no-op — fetch it with get_param
# (aborts boot if the SSM slot is empty/None/CHANGEME), consistent with the other required
# creds above. The operator MUST populate this SecureString slot before deploy (it is already
# populated); rebase-on-behalf + ancestor-atomic submit need it to authenticate.
autolander_gerrit_token="$(get_param autolander-gerrit-token)"

# --- Write the .env atomically (0600), then move into place ----------------
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
  echo "AUTOLANDER_GERRIT_TOKEN=${autolander_gerrit_token}"
  echo "REVIEW_BOT_PORT=8000"
  echo "AUTOLANDER_PORT=8081"
} >"${tmp}"
mv -f "${tmp}" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

echo "fetch-secrets.sh: wrote ${ENV_FILE} (0600) from ${SSM_PREFIX}/* in ${AWS_REGION}" >&2
