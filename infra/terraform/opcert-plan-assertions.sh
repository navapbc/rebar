#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# opcert-plan-assertions.sh — the AC1 structural assertions for the op-cert edge
# (story 76d2). Each sub-claim is a literal, self-failing `jq -e` query against a single
# `terraform show -json tf.plan` output; ANY violation exits non-zero (and this script
# exits non-zero), so it can gate the deploy / CI.
#
# REQUIRES AWS CREDENTIALS: it runs a real `terraform plan`. The OPERATOR runs it (the
# hermetic unit test test_opcert_deploy_infra.py asserts the .tf SOURCE offline).
#
# RUN IT POST-APPLY (idempotency re-plan). Two attributes are computed and only KNOWN in
# planned_values once the resources exist in state:
#   - the integration's `X-Opcert-Guard` header value = random_password.opcert_guard.result
#     (any unknown value nulls the WHOLE request_parameters map in planned_values), and
#   - the invoke policy's Resource = the API execution ARN.
# On a FRESH plan (nothing applied yet) both are known-after-apply, so assertions (b) and the
# invoke-policy check read null. After `terraform apply`, re-running `terraform plan` yields a
# no-change plan whose planned_values carry the now-known values, and every assertion passes.
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

# (a) integration URI is HTTPS (the box's TLS nginx origin, not http://:80).
assert "(a) integration_uri is https://" \
  '.planned_values.root_module.resources[]
     | select(.type == "aws_apigatewayv2_integration")
     | .values.integration_uri | startswith("https://")'

# (b) the static origin-guard request header is injected on the integration.
assert "(b) append:header.X-Opcert-Guard request parameter" \
  '.planned_values.root_module.resources[]
     | select(.type == "aws_apigatewayv2_integration")
     | .values.request_parameters | has("append:header.X-Opcert-Guard")'

# (c+d) both SSM parameters exist and are SecureString.
assert "(c+d) both SecureString SSM params present" \
  '[.planned_values.root_module.resources[]
     | select(.type == "aws_ssm_parameter")
     | {n: .values.name, t: .values.type}]
   | (map(.n) | contains(["/rebar/prod/opcert-ed25519-key", "/rebar/prod/opcert-origin-guard"]))
     and all(.t == "SecureString")'

# (e) EVERY route is SigV4-authenticated (aggregated with all(); a per-route stream would let
#     jq -e pass on the last route alone).
assert "(e) all routes authorization_type == AWS_IAM" \
  '[.planned_values.root_module.resources[]
     | select(.type == "aws_apigatewayv2_route")
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

# (f) the KEY parameter carries `lifecycle { ignore_changes = [value] }` so an apply never
#     clobbers the operator-seeded key. The ticket's literal query reads the configuration
#     section's `.lifecycle_meta_arguments.ignore_changes`. NOTE: `terraform show -json` does
#     NOT emit lifecycle meta-arguments in its configuration representation (verified on
#     Terraform 1.10.x and 1.15.x — the field is simply absent), so that JSON query cannot
#     succeed against current Terraform. We run it as the PRIMARY assertion (it will pass on a
#     Terraform whose JSON schema exposes lifecycle) and, when the field is absent, fall back to
#     a source-contract check that verifies the SAME property in opcert.tf. Both prove the key
#     parameter is guarded by ignore_changes = [value].
f_json='.configuration.root_module.resources[]
          | select(.address == "aws_ssm_parameter.opcert_ed25519_key")
          | .lifecycle_meta_arguments.ignore_changes? // empty | length > 0'
if jq -e "${f_json}" "${PLAN_JSON}" >/dev/null 2>&1; then
  echo "  PASS  (f) key-param lifecycle.ignore_changes (via .lifecycle_meta_arguments)" >&2
else
  # Fallback: assert the source declares ignore_changes = [value] on the key parameter.
  if awk '
      /resource "aws_ssm_parameter" "opcert_ed25519_key"/ { inres = 1 }
      inres && /lifecycle[[:space:]]*{/                    { inlc = 1 }
      inlc && /ignore_changes[[:space:]]*=[[:space:]]*\[[[:space:]]*value[[:space:]]*\]/ { found = 1 }
      inres && /^}/ && !/resource/                         { inres = 0; inlc = 0 }
      END { exit(found ? 0 : 1) }
    ' opcert.tf; then
    echo "  PASS  (f) key-param lifecycle.ignore_changes (source fallback: opcert.tf declares ignore_changes = [value])" >&2
  else
    echo "  FAIL  (f) key-param lifecycle.ignore_changes" >&2
    echo "opcert-plan-assertions: AC1 violation on '(f) lifecycle.ignore_changes'; refusing." >&2
    exit 1
  fi
fi

echo "opcert-plan-assertions: ALL AC1 assertions passed." >&2
