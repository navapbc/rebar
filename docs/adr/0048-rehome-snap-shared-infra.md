# ADR 0048: Re-home snap's shared AWS infrastructure into rebar

## Status
Accepted (2026-07-14). Implemented by epic gaugeable-combatable-skylark (stories
ample-famous-cockatiel = terraform + live migration; likeminded-demandable-racerunner = docs).

## Context
rebar and the `snap` demo project shared one AWS account (`896586841071`). snap was
turned down. Its terraform (`snap-demo-tfstate-896586841071`, key `shared/terraform.tfstate`)
still tracked 18 shared, rebar-facing resources that were left running:

- the **GitHub Actions OIDC provider** (`token.actions.githubusercontent.com`) — an
  account-wide singleton,
- the **`*.solutions.navateam.com` wildcard ACM cert** + its DNS validation, and
- the full **`auth_host` domain-wide SSO stack**: a regional Lambda fronted by API
  Gateway v2 + CloudFront, the `auth.solutions.navateam.com` Route53 records, the
  cookie-signing SSM secret, and the CloudFront↔Lambda origin secret.

rebar **functionally depends** on the OIDC provider: its `rebar-terraform-plan` drift
role (`iam.tf`) federates that provider but never created it — it assumed snap's. Leaving
a live dependency owned by a decommissioned stack is a latent outage: a future
`terraform destroy` of snap's leftover state would delete the provider and break rebar's
OIDC. The wildcard cert + SSO stack were idle orphans in the dead state.

## Decision
rebar **adopts** the shared resources into its own terraform (`infra/terraform/**`,
state `rebar-tfstate-896586841071`) rather than tearing them down, so the domain-wide SSO
survives and rebar owns the dependency it relies on. Mechanics:

1. **Import, don't recreate.** Each live object was `terraform import`ed into rebar's
   state and applied as a retag-only change — no destroy/replace, no secret rotation.
2. **Preserve physical names.** The live resources keep their `snap-demo-*` names, because
   renaming forces destroy+recreate and SSO downtime; the misleading name is a deliberate
   tradeoff and a rename is left as a separate future migration.
3. **Adopt the OIDC provider only, not snap's deploy role.** snap's `github_deploy` role is
   snap-specific; rebar keeps its own `rebar-terraform-plan` role and points its trust at
   the now-managed `aws_iam_openid_connect_provider.github.arn`.
4. **Cookie-signing secret uses rebar's SSM idiom** — an SSM SecureString managed with
   `ignore_changes = [value]` (the "existence + type, not value" contract), with
   `random_password.auth_origin_secret` retained and pinned so neither value rotates.
5. **Release from snap's state.** The 18 resources were `terraform state rm`'d from snap's
   shared state (forgets, never destroys), making rebar the single owner.
6. **Adopt the reusable `sso-gate` module as code.** The auth host mints the cookie and the
   per-subdomain viewer-request gate enforces it, so rebar subdomains can opt into the SSO
   later.

The mechanics an operator needs when touching these resources — the import check, the
`ignore_changes` pins, the `RE-HOMED-TO-REBAR.md` marker, and the operator follow-ups — are
in [infra/runbooks/sso-auth-host.md](../../infra/runbooks/sso-auth-host.md)
§"Re-homing mechanics (from ADR 0048)".

## Consequences
- rebar owns its OIDC dependency; a snap teardown can no longer break rebar's drift CI.
- The domain-wide SSO (`auth.solutions.navateam.com`) survives, rebar-owned and retagged.
- Resource names still read `snap-demo-*` until a future rename migration — see the
  name-preservation note in `auth_sso.tf` and `infra/runbooks/sso-auth-host.md`.
- **Not adopted** (remain snap's, to be torn down with it): the `github_deploy` role,
  VPC/ECS/ECR/ALB/SES/RDS, the audit dashboard, and its `audit_sso_gate` instance. The
  leftover RDS manual snapshot and the `snap-demo-tfstate` bucket are snap's to retire.
