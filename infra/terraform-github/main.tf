# ---------------------------------------------------------------------------
# S6 — GitHub mirror-lock IaC (navapbc/rebar)
#
# GOAL: lock `main` and all tags so ONLY the Gerrit replication deploy key
# (title "rebar-gerrit-replication", registered in S5) can update them. Every
# human/admin push, PR-merge, force-push, deletion, and tag write is rejected.
#
# HOW THE `update` RULE LOCKS OUT PR MERGES
#   A ruleset's `update` rule means "restrict updates": only the ruleset's
#   bypass_actors may update the matched refs. A PR merge into main is itself an
#   *update to main* by a non-bypass actor, so the `update` rule rejects BOTH
#   direct pushes AND PR merges — there is no merge path for a non-bypass actor.
#   `deletion` + `non_fast_forward` additionally block deleting/force-pushing.
#
# THE ONLY BYPASS IS THE DEPLOY KEY
#   bypass_actors is a single entry: { actor_type = "DeployKey",
#   bypass_mode = "always" } with NO actor_id. provider integrations/github
#   >= 6.8.0 supports this native DeployKey bypass (GitHub identifies the repo's
#   deploy-key bypass slot by actor_type alone; it carries no numeric id).
#   On a provider < 6.8.0 this resource will not validate — use the gh-api path
#   infra/github/apply-mirror-lock.sh instead (no provider-version dependency).
#
# MAIN-ONLY (WS7 cutover decision, epic b744):
#   The live cutover locked ONLY `main` (ruleset `gerrit-mirror-lock-main`,
#   id 18402431) and deliberately LEFT TAGS OPEN so `.github/workflows/release.yml`
#   (PyPI publish on a human `v*` tag push) keeps working. The tag_lock resource
#   was therefore REMOVED from this module. To re-lock tags later, re-add it plus a
#   release-tag path (push tags via the deploy key or a CI bypass).
#
# NOT MANAGED HERE (deliberately):
#   * No `github_repository` resource — managing it risks `terraform destroy`
#     DELETING the repo. Repo feature toggles (PRs/Issues/Actions) and the
#     "mirror" banner are runbook/gh-api steps, not Terraform.
#   * The legacy `main-protection` ruleset removed during the WS7 cutover was
#     id **18306946** (not the older `18048287` the snapshot/S6 comments reference —
#     the ruleset was recreated between S6-pre and cutover). It is snapshotted at
#     infra/github/main-protection.snapshot.json; rollback recreates it from the
#     snapshot (see infra/runbooks/github-mirror-lock.md).
#
# STATE: applied out-of-band via gh-api at cutover, then `terraform import`ed so
#   `terraform plan` is clean. Keep it that way — future ruleset changes go through
#   this module (`terraform apply`), not gh-api.
# ---------------------------------------------------------------------------

provider "github" {
  owner = "navapbc"
  # token: omitted on purpose — the provider reads GITHUB_TOKEN from the
  # environment when var.github_token is null. Set GITHUB_TOKEN (a token with
  # Administration:write) before `terraform apply`. NEVER commit a token.
  token = var.github_token
}

# EXISTENCE GATE — the S5 replication deploy key must be present. We read all
# deploy keys and assert it (in a `lifecycle { precondition }` on the lock below).
# Locking with a missing bypass actor would lock EVERYONE out, replication
# included, so the assertion must ABORT the apply (fail-closed), not merely warn.
data "github_repository_deploy_keys" "all" {
  repository = var.repository
}

# Branch lock: restrict updates to `main` to bypass actors only (the deploy
# key). Rejects direct pushes AND PR merges, plus force-push and deletion.
resource "github_repository_ruleset" "main_lock" {
  name        = "gerrit-mirror-lock-main"
  repository  = var.repository
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["refs/heads/main"]
      exclude = []
    }
  }

  rules {
    update           = true # restrict updates -> blocks pushes AND PR merges
    deletion         = true
    non_fast_forward = true # no force-push
  }

  bypass_actors {
    actor_type  = "DeployKey"
    bypass_mode = "always"
    # actor_id intentionally omitted — a DeployKey bypass has no numeric id.
  }

  # FAIL-CLOSED existence gate. Unlike a `check` block (which only emits a WARNING
  # and lets `apply` proceed), a resource `precondition` ABORTS plan/apply when the
  # replication deploy key is absent — so we can never activate a lock whose sole
  # bypass actor does not exist (which would freeze `main` for replication too).
  lifecycle {
    precondition {
      condition = length([
        for k in data.github_repository_deploy_keys.all.keys :
        k if k.title == var.deploy_key_title
      ]) > 0
      error_message = "Replication deploy key '${var.deploy_key_title}' not found on navapbc/${var.repository}. Register it (S5, write-enabled) BEFORE applying the mirror-lock — otherwise the lock's only bypass actor is absent and replication is locked out along with everyone else."
    }
  }
}

output "main_lock_ruleset_id" {
  description = "Ruleset id of the branch lock on main."
  value       = github_repository_ruleset.main_lock.ruleset_id
}
