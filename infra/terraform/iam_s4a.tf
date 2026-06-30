# ---------------------------------------------------------------------------
# IAM — S4a (review-bot identity + event plumbing) on the S1-owned instance role.
# ---------------------------------------------------------------------------
# SINGLE-OWNER CONTRACT (see iam.tf): S1 OWNS the EC2 instance role
# `rebar-gerrit-instance-role`. S4a must NOT recreate it; downstream stories
# ATTACH separately-named scoped inline policies to this same role rather than
# creating a second role.
#
# DECISION: S4a NEEDS NO ADDITIONAL IAM.
#   The only AWS access S4a introduces at runtime is the box (the review-bot
#   receiver) reading the bot token at `/rebar/prod/gerrit-bot-token` from SSM.
#   That path is ALREADY covered by S1's least-privilege grant
#   (`rebar-gerrit-ssm-params-read`), which grants ssm:GetParameter[s] on
#   `arn:aws:ssm:us-east-1:896586841071:parameter/rebar/prod/*` (a scoped prefix,
#   NOT Resource:"*") plus the kms:Decrypt needed for the SecureString. The bot
#   token param sits squarely under that `/rebar/prod/*` prefix, so no new grant
#   is required — adding one would be redundant and would dilute the single
#   prefix grant into two overlapping statements.
#
#   The token's WRITE side (put-parameter, run by service-user.sh) happens from
#   an operator WORKSTATION under the operator's own AWS credentials, not from
#   the instance role — so it needs no addition here either.
#
# This file is intentionally a documentation anchor (a `data` reference + this
# comment, no new grants) so the "S4a reuses S1's /rebar/prod/* SSM-read grant;
# needs no extra IAM" decision is explicit and discoverable rather than an
# unexplained absence. If a later S4a need emerges that the prefix grant does not
# cover, add an `aws_iam_role_policy "rebar-gerrit-s4a-..."` on
# `data.aws_iam_role.instance` (already declared in iam_s2.tf) — never a second
# role.
#
# Note: `data "aws_iam_role" "instance"` is declared once in iam_s2.tf and shared
# across the terraform module, so it is NOT re-declared here (that would be a
# duplicate-resource error).
