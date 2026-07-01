# ADR 0011 — GitHub mirror lock (repo-level ruleset, deploy-key-only writes)

- **Status:** Accepted (epic d251 / story S6-pre; applied in S6)
- **Date:** 2026-06-30

## Context

rebar's `main` is becoming a downstream READ-ONLY mirror of Gerrit (S5 replicates
Gerrit → `navapbc/rebar`). To make Gerrit the *sole* writer, the GitHub repo must
reject every push/PR-merge/force-push to `main` EXCEPT the one from Gerrit's
replication deploy-key identity. This is a one-way-door change to a **public** repo,
so the no-live-change pre-work (snapshot + decision record + scope/backend
confirmation) is captured here (S6-pre) before S6's live apply.

## Decisions

1. **Repo-level ruleset, not org-level.** `navapbc` is a GitHub *free* org, so
   admin-proof *org-level* rulesets (which a paid plan would allow) are not
   available. The lock is therefore a **repo-level** ruleset. Consequence: "no
   human/admin bypass" means no *direct/accidental* pushes and force-push/deletion
   are blocked and proven (an active deploy-key-only ruleset rejects even an admin
   push), but a **trusted repo admin can deliberately edit/disable the rule** — an
   explicitly **accepted PoC risk**.

2. **The single bypass actor is the replication DEPLOY KEY.** The ruleset's bypass
   list contains exactly the deploy key (S5's `rebar-gerrit-replication`), expressed
   as `actor_id: null` + the DeployKey actor type (empirically confirmed canonical).
   All other actors (users, apps, admins) are NOT on the bypass list.

3. **`gh api` for the DeployKey bypass (with a retirement path).** The GitHub *UI*
   omits the DeployKey actor type from the ruleset bypass picker, and
   `terraform-provider-github` historically rejected the DeployKey actor for
   rulesets. So S6 sets the bypass via `gh api` (or, where the provider version
   supports it — `terraform-provider-github` >= 6.8.0 added a native DeployKey
   bypass — via Terraform). **Retirement path:** once the provider's native
   DeployKey bypass is confirmed working on our version, the `gh api` step is
   replaced by the declarative `github_repository_ruleset` resource. S6 prefers the
   native provider path if available and falls back to `gh api` otherwise.

4. **One-way-door nature.** The lock REPLACES the existing human-PR protection model
   (`main-protection`, ruleset 18048287: `pull_request` + `required_status_checks` +
   `deletion` + `non_fast_forward`, admins-bypass-via-PR). After S6, `main` advances
   ONLY via Gerrit replication; the team's contribution workflow moves to Gerrit.
   PRs/Issues/Actions may be disabled as part of the mirror hygiene. This is a
   deliberate, high-blast-radius change.

## Rollback

The pre-existing protection is captured verbatim in
`infra/github/main-protection.snapshot.json` (`gh api
/repos/navapbc/rebar/rulesets/18048287`). Rollback (< 15 min, per the epic): set the
mirror-lock ruleset `enforcement=disabled` (or delete it), recreate `main-protection`
from the snapshot, and re-enable PRs/Issues/Actions. Documented in S6's runbook.

## Token scope + IaC backend (confirmed for S6)

- **Token:** S6's GitHub mutations require an `Administration: write` credential —
  a **fine-grained PAT scoped to `navapbc/rebar` only**, injected into Terraform as a
  `sensitive` variable from a CI secret (or the operator's environment), **never** in
  a committed `.tfvars`. (For this PoC run the operator's `gh` token with the `repo`
  + admin scope is used interactively; the fine-grained PAT is the production path.)
- **Backend:** S6 reuses the shared remote backend confirmed in S1 — the S3 backend
  `rebar-tfstate-896586841071` with S3-native state locking (`use_lockfile=true`,
  no DynamoDB). `terraform plan` before `apply` is mandatory for the high-stakes S6
  apply that replaces the live ruleset.

## Consequences

- S6's live apply is fast, ordered, and reversible because the snapshot + decisions
  + scope are settled here.
- The accepted residual risk (trusted-admin editability on a free org) is documented,
  not hidden.
