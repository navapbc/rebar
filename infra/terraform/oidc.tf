# Account-singleton GitHub Actions OIDC provider shared by the read-only
# Terraform drift role and bridge telemetry role. Terraform owns the
# provider. No long-lived GitHub key is needed for federation.

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # AWS ignores the thumbprint for the GitHub IdP (library-of-trust since 2023),
  # but the API still requires a non-empty value.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = {
    Project = "rebar"
  }
}
