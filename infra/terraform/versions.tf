terraform {
  required_version = ">= 1.10"

  # S3-native state locking via `use_lockfile` (no DynamoDB table needed). This
  # requires Terraform >= 1.10 — the `required_version` floor above guards it.
  #
  # WARNING: Terraform < 1.10 SILENTLY IGNORES `use_lockfile` and runs with NO
  # state locking at all (no error, no warning). Concurrent applies on an old
  # CLI would corrupt the remote state. The required_version constraint is the
  # only thing preventing that — do not lower it.
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
      source  = "hashicorp/aws"
      version = "~> 5.0"
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
