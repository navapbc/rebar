# ---------------------------------------------------------------------------
# IAM — S2 (app config / deploy) scoped grant on the S1-owned instance role.
# ---------------------------------------------------------------------------
# SINGLE-OWNER CONTRACT (see iam.tf): S1 OWNS the EC2 instance role
# `rebar-gerrit-instance-role`. S2 must NOT recreate it; it references the
# existing role by name and would ATTACH a separately-named inline policy for any
# extra permissions the box needs at the app-deploy layer.
#
# DECISION: S2 NEEDS NO ADDITIONAL IAM.
#   Everything S2's boot scripts do at runtime is already covered by the S1 role:
#     - fetch-secrets.sh reads /rebar/prod/* SecureString params (+ kms:Decrypt
#       via SSM) — granted by S1's `rebar-gerrit-ssm-params-read` inline policy.
#     - admin shell access is via SSM Session Manager — granted by S1's
#       AmazonSSMManagedInstanceCore attachment.
#     - pulling the Gerrit image + Let's Encrypt issuance are plain outbound HTTPS
#       (the SG egress), needing no AWS API permissions at all.
#   So there is NO `aws_iam_role_policy "rebar-gerrit-s2-deploy"` to add — adding an
#   empty or speculative policy would violate least privilege.
#
# This file is intentionally a documentation anchor (a `data` reference, no new
# grants) so the "S2 reuses S1's SSM-read grant; needs no extra IAM" decision is
# explicit and discoverable rather than an unexplained absence. If a later S2 need
# emerges (e.g. ECR pull from a private registry, or CloudWatch agent config), add
# an `aws_iam_role_policy "rebar-gerrit-s2-deploy"` on `data.aws_iam_role.instance`
# here — never a second role.

data "aws_iam_role" "instance" {
  name = "rebar-gerrit-instance-role"
}
