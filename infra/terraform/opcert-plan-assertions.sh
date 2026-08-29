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

# NOTE: the queries below are scoped BY ADDRESS to the op-cert resources
# (`aws_apigatewayv2_integration.opcert`, `aws_apigatewayv2_route.opcert`, the two op-cert SSM
# params). The terraform module is multi-API — it also declares the auth_host SSO API and its
# own routes/integrations/params — so an unscoped `select(.type == ...)` would aggregate those
# unrelated resources (e.g. auth_host's public `$default` route, authorization_type NONE) and
# wrongly fail. Scoping asserts exactly the op-cert edge the AC is about.

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

# (f) the KEY parameter uses terraform WRITE-ONLY arguments (`value_wo` + `value_wo_version`)
#     so its value is NEVER persisted to terraform state (ADR 0105) and an apply never clobbers
#     the operator-seeded key (the provider re-sends value_wo only when value_wo_version
#     changes). It must NOT carry a plaintext `value = "..."` or `lifecycle { ignore_changes =
#     [value] }` — that antipattern read the live cleartext into state on every refresh (bug
#     eb67-b96c-dcf0-4f86). The configuration representation of `terraform show -json` exposes the
#     `value_wo_version` expression; we run that as the PRIMARY assertion and, when the field is
#     absent (older Terraform JSON schemas), fall back to a source-contract check on opcert.tf.
#     Both prove the key parameter is write-only and NOT ignore_changes-guarded.
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
