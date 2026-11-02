# S7 attaches a scoped Bedrock invoke policy to the S1-owned instance role.
# The Converse runtime authorizes against bedrock:InvokeModel. Cross-region
# inference needs both profile and underlying foundation-model ARNs. Allow
# regional/global profile prefixes while listing exactly the models rebar
# invokes: the Claude models it converses with and the Titan model it embeds
# with.
# Streaming is exercised from the scoped instance role, not inferred from a
# policy simulator. Keep this grant separate from other workloads.

data "aws_iam_policy_document" "bedrock_converse" {
  statement {
    sid = "InvokeScopedBedrockModels"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*.anthropic.claude-*",
      "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock_converse" {
  name   = "rebar-gerrit-bedrock-converse"
  role   = aws_iam_role.gerrit_instance.id
  policy = data.aws_iam_policy_document.bedrock_converse.json
}
