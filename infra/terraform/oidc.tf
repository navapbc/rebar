# ---------------------------------------------------------------------------
# oidc.tf — GitHub Actions OIDC identity provider (re-homed from snap)
# ---------------------------------------------------------------------------
# The account-wide OIDC provider for GitHub Actions token federation. rebar's
# drift-detection role (rebar-terraform-plan, iam.tf) federates THIS provider to
# run read-only `terraform plan` without long-lived keys. There is exactly ONE
# such provider per account (a singleton keyed on the URL), so it is shared
# account infrastructure — historically created and owned by the now-decommissioned
# `snap` project's terraform. rebar has adopted ownership (epic gaugeable-combatable-
# skylark): the resource was `terraform import`ed from the live provider and released
# from snap's shared state, so rebar no longer depends on a dead project's stack for
# a resource it functionally requires.
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
