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
#   /rebar/prod/mcp-tickets-pat            -> MCP_TICKETS_PAT            (MCP ticket store, OPTIONAL)
#   /rebar/prod/mcp-client-pat-copilot     -> MCP_CLIENT_PAT_COPILOT     (MCP static auth, OPTIONAL)
#   /rebar/prod/mcp-client-pat-codex       -> MCP_CLIENT_PAT_CODEX       (MCP static auth, OPTIONAL)
#   /rebar/prod/mcp-client-pat-claude      -> MCP_CLIENT_PAT_CLAUDE      (MCP static auth, OPTIONAL)
# The two OAuth creds are OPTIONAL here (blank if unpopulated) — they are only needed
# under auth.type = OAUTH, and compose-up.sh FAILS LOUD if OAUTH is selected but they
# are empty. Making them REQUIRED here would couple every boot (incl. non-OAUTH rollback)
# to their presence.
# Plus a non-secret: REVIEW_BOT_PORT=8000 (single-source the port for compose + nginx).
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
# OPTIONAL: the fine-grained GitHub PAT (contents:write on the tickets repo) the mcp
# container's entrypoint feeds to a URL-scoped git credential helper so it can clone the
# `tickets` branch into REBAR_TRACKER_DIR and auto-push the events its tools write. Blank is
# fine: the clone is simply deferred and the container still boots (soft failure posture).
mcp_tickets_pat="$(get_param_optional mcp-tickets-pat)"

# OPTIONAL: the Rebar Bot ed25519 authorship signing key (story 245e). A multi-line
# OpenSSH PEM key cannot live in a single-line .env value, so materialize it to a 0600
# FILE next to the .env (the ci-gerrit-ssh-key / g2p-github-pat materialize-to-file
# precedent) and export only its PATH via REBAR_IDENTITY_SIGNING_KEY in the .env. Blank ⇒
# the reviewbot writes unsigned (its types are gate-exempt, so this is attribution only).
rebar_bot_signing_key="$(get_param_optional rebar-bot-signing-key)"
# ALWAYS create the file, even when the SSM slot is blank (bug beb1). docker creates a
# DIRECTORY when a bind-mount source is missing, so an absent key file would break review-bot
# start now that the key is mounted in. An EMPTY file is the "unsigned" state: rebar treats an
# unreadable/empty key as no key and writes unsigned, which is the documented fallback.
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

# OPTIONAL: the trusted op-cert gate service's passphrase-free Ed25519 PRIVATE signing key
# (story 6f14). Materialized OUTSIDE the app so the app runtime needs no boto3/SSM — the same
# materialize-to-file precedent as rebar-bot-signing-key above. Write the SSM value to a 0600
# FILE next to the .env; the opcert compose service bind-mounts it read-only at the fixed
# container target and points REBAR_OPCERT_KEY_PATH at it. ALWAYS create the file (empty when
# the SSM slot is blank, bug beb1) so the bind-mount source exists — an absent source would make
# docker create a DIRECTORY and break opcert start-up. (An empty file fails compose_signer's
# validation, so a blank slot surfaces as a clear startup error rather than a silent mis-sign.)
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

# OPTIONAL: the per-client MCP bearer PATs (epic jira-reb-3527 "Enable MCP on AWS", ADR 0104 §1).
# One SecureString per client; each client presents its own bearer PAT to the nginx `/mcp/` TLS
# edge and the `static` verifier authenticates it. Two on-box sinks (gotcha f600 — the .env is
# rsync-EXCLUDED, so a rotated SSM value is MATERIALIZED here, not baked into the rsync'd tree):
#   1. the RAW value lands in the 0600 .env as MCP_CLIENT_PAT_* (below, in the .env heredoc);
#   2. the tokens file the verifier reads (mcp-static-tokens.json) references it via `token_env`
#      — env-var NAMES, never a plaintext token, never the raw value in the tokens file (ADR 0050
#      §4 / ADR 0104: the server holds only SHA-256 digests, supplied via env-var names).
# A blank slot's record is OMITTED so `_parse_static_record` never sees an empty token_env; the
# file is ALWAYS written (bug beb1 — a missing bind-mount source would make docker create a
# DIRECTORY). All-blank ⇒ `{"tokens": []}` and the verifier fails-closed at startup ("defines no
# tokens") until an operator populates ≥1 PAT. Rotation is operator-driven (re-materialize +
# RESTART rebar-mcp so the init-time verifier re-reads) — see infra/runbooks/mcp-client-pats.md.
mcp_pat_copilot="$(get_param_optional mcp-client-pat-copilot)"
mcp_pat_codex="$(get_param_optional mcp-client-pat-codex)"
mcp_pat_claude="$(get_param_optional mcp-client-pat-claude)"
mcp_static_tokens_path="$(dirname "${ENV_FILE}")/mcp-static-tokens.json"

# Append one token_env record per POPULATED client (env-var NAMES only; no secret interpolated).
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
  echo "MCP_TICKETS_PAT=${mcp_tickets_pat}"
  # Path (not the key material) to the materialized bot signing key; empty ⇒ unsigned.
  echo "REBAR_IDENTITY_SIGNING_KEY=${signing_key_path}"
  # Per-client MCP bearer PATs (blank ⇒ that client's record was omitted from the tokens file).
  # The tokens file references these env-var NAMES via token_env; the raw values live only here.
  echo "MCP_CLIENT_PAT_COPILOT=${mcp_pat_copilot}"
  echo "MCP_CLIENT_PAT_CODEX=${mcp_pat_codex}"
  echo "MCP_CLIENT_PAT_CLAUDE=${mcp_pat_claude}"
  echo "REVIEW_BOT_PORT=8000"
} >"${tmp}"
mv -f "${tmp}" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

echo "fetch-secrets.sh: wrote ${ENV_FILE} (0600) from ${SSM_PREFIX}/* in ${AWS_REGION}" >&2
