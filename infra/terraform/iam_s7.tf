# ---------------------------------------------------------------------------
# IAM — S7 (review-bot Bedrock access) scoped grant on the S1-owned instance role.
# ---------------------------------------------------------------------------
# SINGLE-OWNER CONTRACT (see iam.tf): S1 OWNS the EC2 instance role
# `rebar-gerrit-instance-role`. S7 must NOT recreate it and must NOT edit
# `iam.tf`; it references the existing role and ATTACHES its own
# separately-named, scoped inline policy here — the same pattern as
# `iam_s2.tf` / `iam_s4a.tf`.
# ---------------------------------------------------------------------------

# Bedrock — the review bot's LLM path (story S7 / ticket 9249). Scoped to ANTHROPIC Claude
# models only, so a call to any other vendor's model is denied (the least-privilege oracle).
#
# ACTION NAME: `bedrock:InvokeModel` — NOT `bedrock:Converse`. This is counter-intuitive and
# was got WRONG once, so the evidence is recorded here rather than left to memory.
#
# rebar reaches Bedrock through pydantic-ai's BedrockProvider, which calls the **Converse**
# API. `bedrock:Converse` does exist as a distinct, simulatable IAM action — which is why
# `aws iam simulate-custom-policy` reports implicitDeny for it under an InvokeModel-only
# grant, and why that simulation LOOKED like proof that Converse was the action to grant.
#
# It is not. The Converse API AUTHORIZES against `bedrock:InvokeModel`. Proven at runtime
# from the instance itself (SSM AWS-RunShellScript on i-00880b2c7f13527c5, running as
# assumed-role/rebar-gerrit-instance-role):
#
#   AccessDeniedException ... is not authorized to perform: bedrock:InvokeModel on resource:
#   arn:aws:bedrock:us-east-1:...:inference-profile/us.anthropic.claude-sonnet-4-6
#   because no identity-based policy allows the bedrock:InvokeModel action
#
# i.e. the policy SIMULATOR answers "what does the policy language permit for the action you
# named", NOT "which action does the service actually check". Only the from-instance call
# distinguishes them. Grant InvokeModel; re-verify from the instance, never from simulation
# alone and never from an operator identity holding bedrock:*.
#
# BOTH resource shapes are required for a cross-region inference profile: the profile ARN AND
# the underlying foundation models in every region the profile can route to. The region field
# is wildcarded; the MODEL is not.
#
# The inference-profile ARN wildcards BOTH the region AND the profile prefix
# (`*.anthropic.claude-*`). The prefix wildcard is load-bearing: `global.` inference profiles
# (e.g. `global.anthropic.claude-sonnet-4-6`) are a valid, MEASURED form in this account, so
# pinning `us.` would silently deny a legitimate model id at runtime.
#
# `InvokeModelWithResponseStream` is EXERCISED, not assumed. It was originally granted by
# symmetry with InvokeModel, which this story's own evidentiary standard forbids, so it was
# verified by a real streaming call made FROM the review-bot container on the instance:
#
#   IDENTITY: arn:aws:sts::896586841071:assumed-role/rebar-gerrit-instance-role/i-00880b2c7f13527c5
#   boto3 bedrock-runtime converse_stream(us.anthropic.claude-sonnet-4-6) -> "ready"
#
# Run from the scoped instance role (never an operator identity holding bedrock:*), using
# boto3 rather than the CLI because aws-cli 2.33.15 on this AMI exposes no streaming
# subcommand at all (`bedrock-runtime` offers only converse / invoke-model / the async trio).
# SCOPE OF THAT PROOF: it shows the streaming path SUCCEEDS under this policy. It does NOT by
# itself identify which IAM action ConverseStream authorizes against, since the policy grants
# both actions; the deny-side control for that is S8's fault injection.
data "aws_iam_policy_document" "bedrock_converse" {
  statement {
    sid = "ConverseAnthropicClaudeOnly"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*.anthropic.claude-*",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock_converse" {
  name   = "rebar-gerrit-bedrock-converse"
  role   = aws_iam_role.gerrit_instance.id
  policy = data.aws_iam_policy_document.bedrock_converse.json
}
