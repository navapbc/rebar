# S4a adds no IAM grant. The S1-owned instance role already reads the bot
# token under /rebar/prod/* through scoped SSM/KMS permissions. The operator
# writes the token from a workstation. Reuse the shared role data reference
# in iam_s2.tf. Do not declare another instance role or duplicate policy.
