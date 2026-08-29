terraform {
  required_version = ">= 1.11"

  # Floor rationale (two constraints, higher wins → >= 1.11):
  #   * >= 1.11 — terraform WRITE-ONLY arguments (`value_wo` / `value_wo_version`), which the
  #     SSM SecureString SECRET slots use so a secret value is NEVER persisted to state
  #     (ADR 0105 / ssm.tf, opcert.tf, auth_sso.tf).
  #   * >= 1.10 — S3-native state locking via `use_lockfile` (no DynamoDB table needed).
  # The >= 1.11 floor above satisfies both.
  #
  # WARNING: Terraform < 1.10 SILENTLY IGNORES `use_lockfile` and runs with NO
  # state locking at all (no error, no warning). Concurrent applies on an old
  # CLI would corrupt the remote state. And Terraform < 1.11 does not support
  # write-only arguments. The required_version constraint is the only thing
  # preventing both — do not lower it.
  #
  # The bucket name matches the one created by infra/bootstrap/main.tf.
  backend "s3" {
    bucket       = "rebar-tfstate-896586841071"
    key          = "rebar/prod/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # >= 5.79 — aws_ssm_parameter write-only arguments (value_wo / value_wo_version).
      version = "~> 5.79"
    }
    # Used by the re-homed auth_host SSO stack: random_password mints the CloudFront↔Lambda
    # origin secret (auth_host.tf); archive_file zips the auth-host Lambda bundle.
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}
