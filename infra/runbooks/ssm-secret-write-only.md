# Runbook — SSM SecureString secrets with write-only arguments (declare · seed · rotate · remediate)

Scope: every operator-seeded SSM **SecureString secret** in `infra/terraform`. Per
[ADR 0105](../../docs/adr/0105-ssm-secrets-never-in-terraform-state.md) these are declared with the
Terraform **write-only arguments** `value_wo` + `value_wo_version`, so the secret value is **never
persisted to terraform state** — extending ADR 0008 (secrets → ephemeral 0600 `.env`, never in
repo/image) to *and never in terraform state either*.

Covers the three seeded-secret resources:

- `aws_ssm_parameter.rebar_secrets` — the 18 `/rebar/prod/*` secret slots (`gerrit-admin-password`,
  `anthropic-api-key`, `rebar-bot-signing-key`, `jira-api-token`, `mcp-client-pat-*`, …).
- `aws_ssm_parameter.opcert_ed25519_key` — `/rebar/prod/opcert-ed25519-key` (ADR 0049 op-cert key).
- `aws_ssm_parameter.cookie_signing_secret` — the auth-SSO cookie signing secret.

**Out of scope** (do not use `value_wo`): non-secret `String` params whose value lives in git
(`jira-url`, `jira-user`, `jira-project`), and the terraform-**generated** SecureString
`aws_ssm_parameter.opcert_origin_guard` (value = `random_password.opcert_guard.result`; its secret is
already in state via the `random_password` resource, so `value_wo` buys nothing). The
`scripts/check_ssm_secret_state.py` guard (wired into `make lint`) encodes exactly this scope and
fails the build if a SecureString secret regresses to `value`/`ignore_changes = [value]`.

## How the parameters are declared

Each secret slot is a write-only parameter — a placeholder `value_wo` seed plus a monotonic
`value_wo_version`, and **no** `value` and **no** `lifecycle { ignore_changes = [value] }`:

```hcl
resource "aws_ssm_parameter" "opcert_ed25519_key" {
  name             = "/rebar/prod/opcert-ed25519-key"
  type             = "SecureString"
  value_wo         = "CHANGEME"   # write-only: never persisted to state
  value_wo_version = 1            # bump to re-send value_wo on the next apply
  tags             = local.tags
}
```

Why the value never reaches state: `value_wo` is a Terraform write-only argument (1.11+, AWS
provider 5.79+). The provider sends it to AWS on create and whenever `value_wo_version` **changes**,
and stores only the integer `value_wo_version` — never the value itself. Contrast the retired
`value = "CHANGEME"` + `ignore_changes = [value]` contract: `ignore_changes` suppressed the *diff*
but not the *refresh*, so every plan/refresh/import read the live cleartext into
`attributes.value` in the remote state (the exposure fixed by ticket `finedrawn-closed-stud` /
`eb67-b96c-dcf0-4f86`).

`versions.tf` declares the floors this needs: `required_version >= 1.11` and AWS provider `~> 5.79`.

## Seed a NEW secret value

For a slot whose current live value is the `CHANGEME` placeholder (freshly created, or just after
the migration cutover below), seed the real value **out-of-band** — the value never goes through git
or terraform:

```sh
aws ssm put-parameter --overwrite --type SecureString \
  --name /rebar/prod/opcert-ed25519-key --value "<real-secret>"
```

Then re-materialize on the box (`infra/scripts/fetch-secrets.sh`) and restart the consumer so it
reads the new value. Do **not** bump `value_wo_version` for an out-of-band seed — leaving the version
unchanged is exactly what keeps a later `apply` from clobbering the seeded value (steady-state
no-clobber).

## ROTATE an existing secret

Two independent ways to rotate; pick per situation:

1. **Out-of-band re-seed (preferred for routine rotation)** — identical to the seed step above:
   `aws ssm put-parameter --overwrite …` with the new value, then re-materialize + restart the
   consumer. This is a value-only change (not a git change), so it does **not** advance `main` and
   the autodeploy timer no-ops on it. Because the value is never in state, this leaves **no** new
   cleartext copy in the state history. Leave `value_wo_version` unchanged.

2. **Terraform-driven rotation (when you want the apply to push the value)** — set the new value as
   `value_wo` and **increment `value_wo_version`** (e.g. `1` → `2`) in the resource, then
   `terraform apply`. The provider re-sends `value_wo` **only because the version changed**, so the
   new value is written to AWS while still never touching state. Never commit a real secret as
   `value_wo` in git — this path is for operator-local apply only; the committed value stays
   `CHANGEME`.

Either way the value never lands in terraform state.

## REMEDIATE the existing exposure (one-time operator cutover)

This is the deferred operator work (a `task` linked `discovered_from` `finedrawn-closed-stud`). Run
it **after** the `value_wo` code change lands, and coordinate with the separate `rubied` apply that
seeds the 4 new MCP-PAT SSM params so the scrub covers those too.

1. **Apply the migration.** `terraform apply` the `value_wo` change. On this **first** apply,
   `value_wo_version` goes absent → `1` for each migrated slot, so the provider sends
   `value_wo = "CHANGEME"` once and **resets every migrated secret to the placeholder**. This is the
   intended, safe reset — you are about to rotate these secrets regardless. `user_data.sh`'s
   `CHANGEME` fail-fast prevents a boot on a placeholder in the gap.

2. **Re-seed / rotate in the same window.** For every affected slot, generate a **new** value
   (rotation — the old values were exposed in cleartext state) and `aws ssm put-parameter --overwrite`
   it. Rotate the crown-jewels explicitly: Gerrit admin password, op-cert Ed25519 signing key,
   rebar-bot signing key, Anthropic API key, `jira-api-token`, and the SSH host keys. Update any
   external system that trusts the old value (Gerrit account, Jira token, bot's registered public
   key, Anthropic console).

3. **Verify no secret is in live state.** After apply + re-seed, confirm the current state carries
   no SecureString value:

   ```sh
   terraform state pull | jq -r '
     .resources[] | select(.type=="aws_ssm_parameter")
     | .instances[].attributes | select(.type=="SecureString")
     | {name, value, value_wo}'
   ```

   Every migrated secret must show `value: null` (and no `value_wo` field — write-only is absent from
   state). Only the generated `opcert_origin_guard` legitimately still resolves via its
   `random_password`.

4. **Scrub the historical cleartext from the state backend.** The prior cleartext persists in the
   retained versions of `s3://rebar-tfstate-896586841071/rebar/prod/terraform.tfstate` (bucket
   versioning, ~149 versions) even after the live state is clean. Because the exposed secrets are
   **rotated** in step 2, the historical copies are of retired values, but they must still be purged:
   permanently delete the affected object versions (`aws s3api list-object-versions` →
   `aws s3api delete-object --version-id …` for each state version containing a cleartext secret), or
   roll the bucket to a fresh key/lifecycle-expire the old versions per your data-handling policy.
   Treat every retired value as compromised regardless.

5. **Close the loop.** Confirm the `make lint` guard (`scripts/check_ssm_secret_state.py`) is green
   on the landed change, and record completion on the operator ticket.

## Back-out

The code change is structural (declaration form only) — reverting a single slot to `value_wo` from a
mistaken `value` is a normal edit gated by the `make lint` guard. There is no back-out for the
one-time state scrub or the secret rotation: rotated secrets stay rotated.

## See also

- [ADR 0105](../../docs/adr/0105-ssm-secrets-never-in-terraform-state.md) — the decision this runbook
  operationalizes.
- [ADR 0008](../../docs/adr/0008-secrets-source-ssm.md) — secrets sourced from SSM into an ephemeral
  0600 `.env`.
- [`infra/runbooks/mcp-client-pats.md`](./mcp-client-pats.md) — the MCP-PAT slots (now write-only).
