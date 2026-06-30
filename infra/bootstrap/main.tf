# ---------------------------------------------------------------------------
# infra/bootstrap/main.tf — ONE-TIME, LOCAL-STATE bootstrap (standalone)
# ---------------------------------------------------------------------------
# This config breaks the state-backend chicken-and-egg: the main config in
# ../terraform uses an S3 backend, but that bucket has to EXIST before the
# backend can be configured. So this tiny config is applied ONCE, by hand,
# with LOCAL state (no backend block), to create the state bucket. Its bucket
# name then feeds the main config's `backend "s3"` block (versions.tf).
#
#   cd infra/bootstrap && terraform init && terraform apply
#
# The bucket carries `prevent_destroy = true`: destroying it would orphan the
# remote state of the main config. Tearing this down REQUIRES an explicit
# state migration of the main config back to local state first, then removing
# the prevent_destroy guard. Do not `terraform destroy` this casually.
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

# Deterministic bucket name keyed on the account id (globally unique enough for
# a single-account PoC; mirrors the name referenced by ../terraform/versions.tf).
resource "aws_s3_bucket" "tfstate" {
  bucket = "rebar-tfstate-896586841071"

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Project   = "rebar"
    Purpose   = "terraform-remote-state"
    ManagedBy = "terraform-bootstrap"
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 (AES256) for simplicity — no KMS key to manage for a PoC.
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "state_bucket_name" {
  description = "Name of the S3 bucket holding the main config's remote state. Feeds ../terraform/versions.tf backend block."
  value       = aws_s3_bucket.tfstate.bucket
}
