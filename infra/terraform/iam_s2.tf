# S2 adds no IAM role or policy. The S1-owned instance role already provides
# scoped /rebar/prod/* SSM/KMS reads and Session Manager access. Image pulls
# and certificate issuance need only outbound HTTPS. This data reference keeps
# ownership explicit without adding speculative grants.

data "aws_iam_role" "instance" {
  name = "rebar-gerrit-instance-role"
}
