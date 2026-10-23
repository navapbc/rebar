# S7 attaches a Claude-only Bedrock policy to the S1-owned instance role.
# The Converse runtime authorizes against bedrock:InvokeModel. Cross-region
# inference needs both profile and underlying foundation-model ARNs. Allow
# regional/global profile prefixes while constraining the Anthropic model.
# Streaming is exercised from the scoped instance role, not inferred from a
# policy simulator. Keep this grant separate from other workloads.

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
