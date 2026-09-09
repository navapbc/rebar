# GitHub mirror lock for navapbc/rebar.
#
# This module locks only `main`. The `update` rule rejects direct pushes and
# PR merges by every non-bypass actor. Deletion and non-fast-forward rules also
# block branch deletion and force-pushes.
#
# The `rebar-gerrit-replication` DeployKey is the sole bypass. The
# integrations/github provider at 6.8.0 or newer expresses it without actor_id.
# For older providers, use infra/github/apply-mirror-lock.sh. That fallback also
# locks tags, while this module leaves them open for human `v*` release pushes.
#
# This module intentionally has no `github_repository` resource because
# Terraform destroy could delete the repository. Manage feature toggles and the
# mirror banner through the runbook and GitHub API.
#
# Rollback restores `main-protection` from
# infra/github/main-protection.snapshot.json.
#
# The ruleset was created through the GitHub API and imported. Terraform now
# owns later ruleset changes, so apply them through this module.

provider "github" {
  owner = "navapbc"
  # token: omitted on purpose — the provider reads GITHUB_TOKEN from the
  # environment when var.github_token is null. Set GITHUB_TOKEN (a token with
  # Administration:write) before `terraform apply`. NEVER commit a token.
  token = var.github_token
}

# Fail closed unless the replication deploy key exists because it is the sole bypass.
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

  # A resource precondition aborts plan and apply. A check block would only warn.
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
