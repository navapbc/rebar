# ADR 0012: AWS IaC foundations for the Gerrit + rebar review-bot PoC

- **Status:** Accepted
- **Context:** Epic *stand up AWS-hosted Gerrit + rebar review-bot (PoC)*, story
  *S1 — IaC foundations* (this story owns the Terraform skeleton: state backend,
  network, the EC2 instance role + DLM lifecycle policy, the SSM secret slots, the
  instance + data volume + EIP + DNS).

## Context

This is a proof-of-concept standing up a single Gerrit host on AWS (account
896586841071, us-east-1) with a rebar review-bot, fronted by
`rebar.solutions.navateam.com`. The instance is t4g.large (Graviton/arm64),
AL2023, IMDSv2-required, administered solely through SSM Session Manager (no
inbound SSH). The IaC lives in `infra/` (excluded from the Python sdist/wheel).

Four foundational decisions needed to be made and recorded so downstream stories
(S2 app config, S4a bot policies, S7 monitoring) build on a stable contract
rather than re-litigating or duplicating them.

## Decision

### 1. S3 state-bucket bootstrap (chicken-and-egg)

The main config (`infra/terraform/`) uses an S3 remote-state backend, but that
bucket must exist before the backend can initialize. We break the cycle with a
**separate one-time, local-state config** at `infra/bootstrap/main.tf` that has
**no backend block** and creates only the state bucket
(`rebar-tfstate-896586841071`) — versioned, encrypted (SSE-S3/AES256), with all
public access blocked, and `lifecycle { prevent_destroy = true }`. It is applied
once by hand; its bucket name then feeds the main config's `backend "s3"` block.
Tearing the bootstrap down requires an explicit state migration of the main
config back to local state first — destroying it casually would orphan the
remote state.

### 2. S3-native locking via `use_lockfile`, no DynamoDB (gated on TF >= 1.10)

The backend uses `use_lockfile = true` (S3-native conditional-write locking)
instead of the legacy `dynamodb_table`. This removes a whole resource (and its
ownership/cost) from the PoC. The catch: `use_lockfile` only works on
**Terraform >= 1.10** — **older CLIs SILENTLY IGNORE it and run with NO state
locking** (no error, no warning). `required_version = ">= 1.10"` in
`versions.tf` (and the bootstrap) is the guard that prevents an old CLI from
running unlocked; it must not be lowered.

### 3. The `/rebar/prod/*` SSM parameter naming contract

All runtime secrets live as SSM SecureString parameters under the
`/rebar/prod/*` namespace. The instance role's read policy is scoped to exactly
that prefix (no account-wide wildcard). The **seven** parameter names are fixed:

- `/rebar/prod/gerrit-admin-password`
- `/rebar/prod/gerrit-ssh-host-ed25519-key`
- `/rebar/prod/github-replication-deploy-key`
- `/rebar/prod/mcp-hmac-signing-key`
- `/rebar/prod/anthropic-api-key`
- `/rebar/prod/alert-endpoint`
- `/rebar/prod/gerrit-bot-token`

Terraform creates them as **placeholders** with value `"CHANGEME"` and
`lifecycle { ignore_changes = [value] }`, so an operator's out-of-band real
value is never reverted. Terraform owns the parameter's existence and type, not
its value. `user_data.sh` **fails fast** if any fetched value is still
`"CHANGEME"`, so the box never boots with a half-configured secret.

### 4. Single-owner contract for the instance role + DLM policy

The EC2 instance role (`rebar-gerrit-instance-role`) + its instance profile, and
the DLM lifecycle role (`rebar-dlm-lifecycle-role`) + the
`aws_dlm_lifecycle_policy`, are created **only here in S1**. Downstream stories
must not redeclare them:

- **S2 / S4a** ATTACH their own *separately-named, scoped inline policies* to the
  existing instance role — they do not create another role.
- **S7** only MONITORS the DLM snapshots/alarms — it does not declare a second
  DLM policy or role.

One resource, one owner, avoids the "two configs fight over the same resource"
drift class.

## Other safeguards recorded here

- **`prevent_destroy`** on the three irreplaceable resources: the state bucket,
  the Gerrit data volume (`rebar-gerrit-data`, a separate gp3 EBS volume, not the
  root), and the Elastic IP — so a `terraform destroy` or instance replacement
  never silently takes data, state, or the public address with it.
- **`ignore_changes = [ami]`** on the instance: the AMI is resolved from the
  public SSM AL2023-arm64 parameter (not hardcoded), but a newly published AMI id
  must not force-replace the running host on every apply. Replacement is explicit.
- **NVMe-by-volume-id device resolution.** Nitro/Graviton surfaces the EBS data
  volume as `/dev/nvme*n1`, not the `/dev/sdf` requested in the attachment.
  `user_data.sh` resolves the real device by matching the (dash-stripped) volume
  id against the NVMe controller serial (with a `/dev/disk/by-id` fallback), then
  formats idempotently and mounts at `/var/gerrit` by UUID in `/etc/fstab`.
- **Placeholder-secret fail-fast** (see decision 3) — cloud-init exits non-zero
  on a `CHANGEME` value rather than writing a broken `/etc/rebar/.env`.
- **Default VPC** is a deliberate PoC simplification: all subnets are public, and
  the security group (HTTPS 443 + Gerrit SSH 29418, no port 22) is the security
  boundary.

## Consequences

- Bringing up the environment is a two-step apply: `infra/bootstrap` once
  (local state), then `infra/terraform` (remote state). Operators must populate
  the seven `/rebar/prod/*` secrets before the instance-bringing apply or
  cloud-init fails fast.
- Downstream stories have a stable, documented contract: the parameter
  namespace, the single-owner role/DLM resources, and the prevent_destroy /
  ignore_changes guards are fixed here and referenced, not duplicated.
- A production hardening pass (private subnets + load balancer, KMS-CMK state
  encryption, tighter ingress CIDRs) is explicitly out of scope for this PoC.
