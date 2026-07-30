# Runbook — verifying the review bot's Bedrock access (operator steps)

The review bot reaches Claude through **AWS Bedrock** using the EC2 instance role
`rebar-gerrit-instance-role` (no rebar-managed key — the ambient credential chain). This runbook is how you
verify that access after changing it, and — more importantly — **how not to verify it**, because two plausible
methods silently prove nothing.

Terraform owns the grant: `infra/terraform/iam.tf`, `aws_iam_role_policy.bedrock_converse`.

---

## The action name is `bedrock:InvokeModel`, not `bedrock:Converse`

rebar calls the Bedrock **Converse** API (via pydantic-ai's `BedrockProvider`). Counter-intuitively, Converse
**authorizes against `bedrock:InvokeModel`**.

`bedrock:Converse` does exist as a distinct, simulatable IAM action, so a policy simulator will happily tell you
it is denied — which reads as proof that you must grant it. That inference is wrong and has already been made
once on this system. Grant `bedrock:InvokeModel` (+ `bedrock:InvokeModelWithResponseStream` for streaming).

---

## How NOT to verify

**Do not verify from your own workstation identity.** An operator holding `AdministratorAccess` or
`bedrock:*` will succeed at every call regardless of what the *role* can do. A wildcard identity cannot fail,
so its success carries zero information about the grant.

**Do not stop at `simulate-principal-policy`.** It answers *"would this policy permit the action I named?"* —
not *"which action does the service actually check?"*. It is necessary (it catches policy-language and
resource-ARN errors) but **not sufficient** for an authorization contract.

**Do not check only the positive case.** Without a negative control you have not shown the Claude-only scope
binds; you have shown the role can call *something*.

---

## How to verify — three layers, all required

### 1. Policy layer — `simulate-principal-policy` against the ROLE

```sh
ROLE=arn:aws:iam::<account>:role/rebar-gerrit-instance-role
PROFILE_ARN=arn:aws:bedrock:us-east-1:<account>:inference-profile/us.anthropic.claude-sonnet-4-6

aws iam simulate-principal-policy \
  --policy-source-arn "$ROLE" \
  --action-names bedrock:InvokeModel bedrock:InvokeModelWithResponseStream \
  --resource-arns "$PROFILE_ARN" \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text
```

Expect `allowed` for both. A `implicitDeny` here means the policy or the resource ARN shape is wrong.

### 2. Runtime layer — a real call FROM the instance (the one that actually settles it)

```sh
aws ssm send-command --instance-ids <instance-id> \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["aws bedrock-runtime converse --region us-east-1 \
     --model-id us.anthropic.claude-sonnet-4-6 \
     --messages '"'"'[{\"role\":\"user\",\"content\":[{\"text\":\"Reply with the single word: ready\"}]}]'"'"' \
     --inference-config '"'"'{\"maxTokens\":16}'"'"' \
     --query \"output.message.content[0].text\" --output text 2>&1 | tail -2"]'
```

Then `aws ssm get-command-invocation --command-id <id> --instance-id <instance-id>`. Expect the model's reply
(e.g. `ready`). This is the only layer that reveals which IAM action the service authorizes against, and it is
what caught the wrong grant.

### 3. Negative control — a non-Claude model must be DENIED

Repeat step 2 with `--model-id amazon.nova-pro-v1:0`. Expect:

```
AccessDeniedException ... is not authorized to perform: bedrock:InvokeModel
on resource: arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0
```

If this *succeeds*, the scope is too broad — the policy is meant to permit `anthropic.claude-*` only.

---

## Which model id to use

**Inference-profile ids only** (`us.anthropic.claude-…`). Plain on-demand ids
(`anthropic.claude-sonnet-4-6`) return `ValidationException: … on-demand throughput isn't supported. Retry
your request with the ID or ARN of an inference profile.` The shipped default is
`rebar.llm.config.DEFAULT_BEDROCK_MODEL_ID`.

## Checking prompt caching actually works

Caching is **model-dependent and fails silently** — an ineffective cache reports `cache_read=0` and
`cache_write=0` while billing the full input on every call, with no error. rebar warns when caching was
requested and both counters are zero (see `structured_run.warn_if_cache_ineffective`), but to check directly,
issue the same request twice with a stable prefix ≥ ~1k tokens and read `cacheReadInputTokens` on the second.
Measured working: `us.anthropic.claude-sonnet-4-6`. Measured NOT caching: `us.`/`global.`
`claude-opus-4-5-20251101-v1:0`.

## `temperature` is rejected by some models

`us.anthropic.claude-opus-4-7` returns `400 … 'temperature' is deprecated for this model`; the same call
without it succeeds. pydantic-ai drops unsupported sampling settings only on its Anthropic adapter, not its
Bedrock one, so rebar carries this per-model in
`rebar.llm.capabilities._MODEL_ID_CAPABILITY_OVERRIDES`. If a new model 400s on a sampling parameter, add its
exact id there rather than dropping temperature globally — greedy (`temperature=0`) Pass-2 verification depends
on it.
