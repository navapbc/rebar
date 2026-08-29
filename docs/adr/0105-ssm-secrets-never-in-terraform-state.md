# ADR 0105: SSM SecureString secrets use write-only arguments — secret values are never persisted to terraform state

- **Status:** Accepted
- **Date:** 2026-08-29
- **Context:** Bug *Terraform state stores every SSM SecureString value in cleartext, collapsing the
  secret tier to the state-bucket ACL* (`eb67-b96c-dcf0-4f86` / `finedrawn-closed-stud`). Extends
  ADR 0008 (secrets sourced from SSM into an ephemeral 0600 `.env`, never in repo/image) to *and
  never in terraform state either*; builds on the S1/ADR-0012 SSM SecureString substrate; relates to
  ADR 0046 (security posture / accepted limitations).

## Context

ADR 0008 established that runtime secrets live as **SSM Parameter Store SecureString** params under
`/rebar/prod/*` (and `/auth-solutions/*`), read on-box by the instance role into an ephemeral 0600
`.env`, so a secret never reaches the repo or an image layer. The terraform that *provisions* those
parameters used a placeholder contract: `value = "CHANGEME"` + `lifecycle { ignore_changes = [value]
}`. The intent was "terraform owns the parameter's existence and type, not its value" — the operator
seeds the real value out-of-band and a later `apply` never reverts it.

That contract stops terraform **writing** a secret from git, but not terraform **reading** one into
state. `ignore_changes` suppresses the *diff*, not the *refresh*: on every plan/refresh (and on
`import` adoption) the AWS provider reads the live `SecureString` value into `attributes.value`, so
the cleartext lands in the remote state. Bug `eb67` proved this on the production state (S3
`rebar-tfstate-896586841071`, key `rebar/prod/terraform.tfstate`): `terraform state pull` renders 23
secrets — the Gerrit admin password, the op-cert Ed25519 signing key, the rebar-bot signing key, the
Anthropic API key, SSH host keys, the Jira API token, the MCP client PATs — in cleartext to anyone
who can read the state backend. The backend is encrypted at rest with **SSE-S3 (AES256), not a KMS
CMK**, and carries no bucket policy, so read access is governed solely by IAM identity policies and
there is no per-decrypt data-event trail; bucket versioning (149 retained versions) means rotating a
secret does not remove its prior cleartext from the state history. The exposure is contained to
account administrators (the EC2 instance role cannot read the state), but it still collapses the
`SecureString` boundary down to the state-bucket ACL for no benefit.

The AWS provider offers a purpose-built mechanism for exactly this: **write-only arguments**.
`value_wo` (paired with `value_wo_version`) is, by design, **never stored to state** — the provider
sends it to AWS on create and whenever `value_wo_version` changes, and stores only the version
integer, never the value. Write-only arguments require **Terraform ≥ 1.11** and, on
`aws_ssm_parameter`, **AWS provider ≥ 5.79** (the deployment runs Terraform 1.15.8 and provider
5.100.0, so both floors are already met once `versions.tf` is raised to declare them).

## Decision

**Secret values must never be persisted to terraform state.** Every operator-seeded SSM
`SecureString` **secret** is declared with write-only arguments — `value_wo = "CHANGEME"` +
`value_wo_version = 1` — and carries **no** `value` argument and **no** `lifecycle { ignore_changes =
[value] }`. This applies to the three seeded-secret resources: `aws_ssm_parameter.rebar_secrets`
(the 18 `/rebar/prod/*` secret slots), `aws_ssm_parameter.opcert_ed25519_key`, and
`aws_ssm_parameter.cookie_signing_secret`. `versions.tf` is pinned to `required_version >= 1.11` and
AWS provider `~> 5.79` to declare the floors the mechanism needs.

Two categories are deliberately **out of scope**:

- **Non-secret `String`/`StringList` params** (`/rebar/prod/jira-url`, `jira-user`, `jira-project`)
  — their values legitimately live in git/config and are not secrets, so they keep plain `value`.
- **Terraform-GENERATED `SecureString` values**, specifically `aws_ssm_parameter.opcert_origin_guard`
  whose value is `random_password.opcert_guard.result`. Its secret already lives in state via the
  `random_password` resource (and the API-Gateway integration header), so `value_wo` would remove it
  from one place while it persists in another — no net benefit. This case is left as-is and is
  explicitly allowed by the guard.

A **hermetic CI guard**, `scripts/check_ssm_secret_state.py`, wired into `make lint`, enforces the
decision structurally: for every `aws_ssm_parameter` of `type = "SecureString"` it fails the build on
a quoted string-literal `value`, on `ignore_changes = [value]`, or on a `value_wo`/`value_wo_version`
pairing error — while allowing the generated-value case. It parses HCL text only; it never contacts
AWS and never reads or prints a secret value. This is the SSM-secret analogue of the existing
`scripts/check_templatefile_escapes.py` terraform gate.

### The one-time cutover is a deliberate reset, run with rotation

Migrating an **existing** seeded parameter from `value` to `value_wo` + `value_wo_version = 1` is not
free of behavior: on the first `apply`, `value_wo_version` transitions from absent to `1` (a change),
so the provider sends `value_wo = "CHANGEME"` once and **resets the live value to the placeholder**.
This is accepted because the same exposure requires **rotating** the already-exposed secrets anyway:
the migration `apply` and the re-seed/rotation are done in one operator maintenance window (apply →
every affected slot is `CHANGEME` → seed the new rotated value out-of-band), with `user_data.sh`'s
`CHANGEME` fail-fast as the safety net against a boot on a placeholder. Steady-state behavior after
that is strict no-clobber: the write-only value is re-sent only when `value_wo_version` is bumped, so
a later `apply` never reverts an out-of-band-seeded value. The operator procedure — including the
existing-state scrub for the historical cleartext — lives in
[`infra/runbooks/ssm-secret-write-only.md`](../../infra/runbooks/ssm-secret-write-only.md).

## Consequences

- **The `SecureString` boundary is restored.** A principal who can read the state backend can no
  longer read the seeded secrets from it; the values live only in SSM (KMS-encrypted) and the
  on-box 0600 `.env`, exactly as ADR 0008 intended. The state-bucket ACL is no longer the effective
  secret tier.
- **Rotation no longer leaves a state tail.** Because the value is never written to state, rotating
  a secret out-of-band leaves no new cleartext copy in the state history (the pre-existing 149-version
  historical tail is scrubbed once by the operator remediation; see the runbook).
- **The decision is enforced, not just documented.** `make lint` fails closed if any future
  `SecureString` secret reintroduces `value`/`ignore_changes = [value]`, so the regression cannot
  land silently. The guard is portable (pure-Python, no CI provider or live AWS required).
- **A one-time operator cutover is required** (tracked as a deferred operator task linked
  `discovered_from` `eb67`): apply the migration, re-seed/rotate the exposed crown-jewel secrets, and
  scrub the existing cleartext from the remote state and its retained versions. Until that runs, the
  historical exposure documented in `eb67` persists; the code change alone does not mutate prod.
- **A minor version floor rises.** `versions.tf` now requires Terraform ≥ 1.11 and AWS provider
  ≥ 5.79; both are satisfied by the current toolchain, and the ≥ 1.11 floor still guards the
  `use_lockfile` state-locking behavior (which needed ≥ 1.10).

## Prior art / grounding

- **ADR 0008** — secrets sourced from SSM into an ephemeral 0600 `.env`, never in repo/image. This
  ADR extends that boundary to terraform state.
- **ADR 0012** — the S1 IaC foundations that provisioned the instance role and the `/rebar/prod/*`
  SSM SecureString slots.
- **ADR 0046** — security posture and accepted limitations; this ADR closes a gap rather than
  accepting it.
- **Terraform write-only arguments** — HashiCorp Terraform 1.11+ ephemeral/write-only arguments; the
  `hashicorp/aws` provider's `aws_ssm_parameter` `value_wo`/`value_wo_version` (provider 5.79+):
  "write-only values are never stored to state," and `value_wo_version` is required with `value_wo`
  and triggers a re-send when incremented.
- **HashiCorp guidance** — "Sensitive data in state": a `SecureString`'s unencrypted value is stored
  in plaintext in raw state when set via `value`; write-only arguments are the recommended avoidance.
- **`scripts/check_templatefile_escapes.py`** — the existing terraform structural gate whose
  script+test+`make lint` wiring pattern this change mirrors.
