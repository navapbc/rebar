terraform {
  required_version = ">= 1.10"

  # S3-native state locking via `use_lockfile` (no DynamoDB). Requires
  # Terraform >= 1.10 — the required_version floor above guards it.
  #
  # SEPARATE STATE KEY from the AWS stack (rebar/prod/terraform.tfstate) so the
  # GitHub config and the AWS/Gerrit config never collide in one state file.
  # Same bucket (created by infra/bootstrap/main.tf), different key.
  backend "s3" {
    bucket       = "rebar-tfstate-896586841071"
    key          = "rebar/prod/github.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    github = {
      source = "integrations/github"
      # >= 6.8.0 is REQUIRED: that release added the native DeployKey bypass
      # actor (`actor_type = "DeployKey"` with no actor_id) used by the locks in
      # main.tf. On an older provider, `terraform validate`/`apply` rejects the
      # DeployKey bypass — fall back to infra/github/apply-mirror-lock.sh
      # (the gh-api path), which has no provider-version dependency.
      version = ">= 6.8.0"
    }
  }
}
