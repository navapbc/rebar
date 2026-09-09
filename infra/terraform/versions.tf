terraform {
  required_version = ">= 1.11"

  # Terraform >= 1.11 provides the write-only SSM values used by ssm.tf, opcert.tf, and
  # auth_sso.tf under ADR 0105. It also satisfies >= 1.10 for S3-native `use_lockfile` locking.
  #
  # Do not lower the floor. Versions below 1.10 ignore locking without warning and risk
  # concurrent state corruption. Versions below 1.11 cannot parse write-only arguments.
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
    # auth_host uses random for its origin secret and archive for its Lambda bundle.
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
