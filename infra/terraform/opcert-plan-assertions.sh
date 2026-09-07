#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verify the op-cert edge with self-failing `jq -e` assertions against one
# `terraform show -json` plan. Any violation exits non-zero for deploy/CI gating.
#
# Requires AWS credentials. test_opcert_deploy_infra.py covers the source offline.
#
# Run it post-apply against a no-change re-plan. The integration's generated
# `X-Opcert-Guard` value and the API execution ARN are unknown on a fresh plan. Those unknowns
# null the request-parameter map and invoke-policy resource needed by their assertions.
#
# Usage:
#   cd infra/terraform
#   terraform apply -var 'opcert_admin_principal_arns=["arn:aws:iam::<acct>:user/ops"]'
#   ./opcert-plan-assertions.sh          # post-apply verification
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")"

PLAN_FILE="${PLAN_FILE:-tf.plan}"
PLAN_JSON="${PLAN_JSON:-tf.plan.json}"

echo "opcert-plan-assertions: terraform plan -> ${PLAN_FILE}" >&2
terraform plan -out="${PLAN_FILE}"
terraform show -json "${PLAN_FILE}" >"${PLAN_JSON}"

# assert <label> <jq-filter> : run `jq -e` against the plan JSON; non-zero => fail loudly.
assert() {
  local label="$1" filter="$2"
  if jq -e "${filter}" "${PLAN_JSON}" >/dev/null; then
    echo "  PASS  ${label}" >&2
  else
    echo "  FAIL  ${label}" >&2
    echo "opcert-plan-assertions: AC1 violation on '${label}'; refusing." >&2
    exit 1
  fi
}

# Scope queries by resource address. This multi-API module also has auth_host resources whose
# public `$default` route and `NONE` auth would make type-only aggregation fail incorrectly.

# (a) integration URI is HTTPS (the box's TLS nginx origin, not http://:80).
assert "(a) integration_uri is https://" \
  '.planned_values.root_module.resources[]
     | select(.address == "aws_apigatewayv2_integration.opcert")
     | .values.integration_uri | startswith("https://")'

# (b) the static origin-guard request header is injected on the integration.
assert "(b) append:header.X-Opcert-Guard request parameter" \
  '.planned_values.root_module.resources[]
     | select(.address == "aws_apigatewayv2_integration.opcert")
     | .values.request_parameters | has("append:header.X-Opcert-Guard")'

# (c+d) both op-cert SSM parameters exist and are SecureString (scoped to the two op-cert params).
assert "(c+d) both SecureString SSM params present" \
  '[.planned_values.root_module.resources[]
     | select(.address == "aws_ssm_parameter.opcert_ed25519_key"
              or .address == "aws_ssm_parameter.opcert_origin_guard")
     | {n: .values.name, t: .values.type}]
   | (map(.n) | contains(["/rebar/prod/opcert-ed25519-key", "/rebar/prod/opcert-origin-guard"]))
     and length == 2 and all(.t == "SecureString")'

# (e) EVERY op-cert route is SigV4-authenticated (aggregated with all(); a per-route stream would
#     let jq -e pass on the last route alone). Scoped to the op-cert API's route(s).
assert "(e) all op-cert routes authorization_type == AWS_IAM" \
  '[.planned_values.root_module.resources[]
     | select(.address | startswith("aws_apigatewayv2_route.opcert"))
     | .values.authorization_type]
   | length > 0 and all(. == "AWS_IAM")'

# (g) the hashicorp/random provider is configured (the guard generator).
assert "(g) random provider configured" \
  '.configuration.provider_config | has("random")'

# invoke-policy: EXACTLY ONE role/managed policy grants execute-api:Invoke, and it is the
#               resource labelled `opcert_admin_invoke` (the rebar-opcert-admin inline policy).
assert "invoke: exactly one execute-api:Invoke policy, labelled opcert_admin_invoke" \
  '[.planned_values.root_module.resources[]
     | select(.type == "aws_iam_role_policy" or .type == "aws_iam_policy")
     | {label: .name, s: (.values.policy | fromjson | .Statement[])}
     | select([.s.Action] | flatten | any(. == "execute-api:Invoke"))]
   | length == 1 and .[0].label == "opcert_admin_invoke"'

# (f) The operator-seeded key uses `value_wo` + `value_wo_version`. Its value never enters
# state or gets resent until the version changes. Plaintext `value` and `ignore_changes =
# [value]` are forbidden. Prefer the plan's expression. Older JSON schemas use the source check.
f_json='.configuration.root_module.resources[]
          | select(.address == "aws_ssm_parameter.opcert_ed25519_key")
          | .expressions.value_wo_version? // empty | length > 0'
if jq -e "${f_json}" "${PLAN_JSON}" >/dev/null 2>&1; then
  echo "  PASS  (f) key-param write-only value_wo_version (via .expressions)" >&2
else
  # Fallback: assert the source declares value_wo + value_wo_version and does NOT declare
  # ignore_changes = [value] on the key parameter.
  if awk '
      /resource "aws_ssm_parameter" "opcert_ed25519_key"/ { inres = 1 }
      inres && /value_wo[[:space:]]*=/                     { has_wo = 1 }
      inres && /value_wo_version[[:space:]]*=/             { has_wover = 1 }
      inres && /ignore_changes[[:space:]]*=[[:space:]]*\[[[:space:]]*value[[:space:]]*\]/ { bad = 1 }
      inres && /^}/ && !/resource/                         { inres = 0 }
      END { exit((has_wo && has_wover && !bad) ? 0 : 1) }
    ' opcert.tf; then
    echo "  PASS  (f) key-param write-only (source fallback: opcert.tf declares value_wo + value_wo_version, no ignore_changes)" >&2
  else
    echo "  FAIL  (f) key-param write-only value_wo/value_wo_version" >&2
    echo "opcert-plan-assertions: AC1 violation on '(f) write-only value_wo'; refusing." >&2
    exit 1
  fi
fi

echo "opcert-plan-assertions: ALL AC1 assertions passed." >&2
