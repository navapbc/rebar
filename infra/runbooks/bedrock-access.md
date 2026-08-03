# Runbook — verifying the review bot's Bedrock access (operator steps)

The review bot reaches Claude through **AWS Bedrock** using the EC2 instance role
`rebar-gerrit-instance-role` (no rebar-managed key — the ambient credential chain). This runbook is how you
verify that access after changing it, and — more importantly — **how not to verify it**, because two plausible
methods silently prove nothing.

> **Not the CI path.** The instance role documented here is **not reachable from a GitHub-hosted
> runner** — there is no instance role on `ubuntu-latest` and no IMDS route to the Gerrit host. The
> external suite's Bedrock arm assumes its own OIDC-federated role instead; see
> [bedrock-ci-oidc.md](bedrock-ci-oidc.md). The two share a policy shape, not a delivery path.

Terraform owns the grant: `infra/terraform/iam_s7.tf`, `aws_iam_role_policy.bedrock_converse`. It is NOT in
`iam.tf` — that file is owned by story S1 under a single-owner contract (`iam.tf:4-9`) and downstream stories
attach their own separately-named scoped policies from their own file, the `iam_s2.tf` / `iam_s4a.tf` pattern.

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

**Do not pass `--region` / `region_name=` and call the region question settled.** Every probe in this
runbook names a region EXPLICITLY, so none of them exercises *ambient* region resolution — which is
exactly how ticket a574 went unnoticed until the container was tested directly. An explicit region
proves the grant works; it proves nothing about whether the service can find a region on its own.

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

### 3. Streaming layer — `converse_stream` must also be exercised, not assumed

`bedrock:InvokeModelWithResponseStream` is granted alongside `InvokeModel`. Do NOT take it on trust by
symmetry — that is the same assumption-by-symmetry that produced the wrong action name in the first place.

The CLI cannot do this: **aws-cli 2.33.15 on this AMI exposes no streaming subcommand** (`aws bedrock-runtime`
offers only `converse`, `invoke-model`, and the async trio), and an unknown subcommand exits printing the CLI
help stub — which is easy to misread as a failed call rather than a missing command. Use boto3 inside the
review-bot container, which is also the truest test since it is the runtime that actually makes the calls:

```sh
cat > /tmp/vs.py <<'EOF'
import boto3
print("IDENTITY:", boto3.client("sts", region_name="us-east-1").get_caller_identity()["Arn"])
c = boto3.client("bedrock-runtime", region_name="us-east-1")
r = c.converse_stream(
    modelId="us.anthropic.claude-sonnet-4-6",
    messages=[{"role": "user", "content": [{"text": "Reply with the single word: ready"}]}],
    inferenceConfig={"maxTokens": 16},
)
print("STREAM_OK:", "".join(
    ev["contentBlockDelta"]["delta"].get("text", "")
    for ev in r["stream"] if "contentBlockDelta" in ev).strip())
EOF
# ship it to the instance via SSM (base64 the script — shell quoting through
# AWS-RunShellScript will otherwise mangle the embedded JSON), then:
docker exec -i compose-review-bot-1 python - < /tmp/vs.py
```

Expect the identity to be `assumed-role/rebar-gerrit-instance-role/<instance-id>` — if it is anything else you
are testing the wrong principal and the result is meaningless — and `STREAM_OK: ready`.

**What this proves and what it does not.** It proves the streaming path SUCCEEDS under this policy from the
scoped role. It does NOT identify which IAM action `ConverseStream` authorizes against, because the policy
grants both actions at once; only removing one and re-testing would show that, which is the deny-side control
S8's fault injection performs.

### 4. Negative control — a non-Claude model must be DENIED

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

---

## The region is NOT discovered — set it explicitly

**MEASURED in `compose-review-bot-1` (account 896586841071):**

```
env | grep '^AWS_'                    -> (nothing: no AWS_REGION, no AWS_DEFAULT_REGION)
env | grep '^REBAR_LLM_'              -> NO_REBAR_LLM_ENV  (so REBAR_LLM_BEDROCK_REGION is unset)
boto3.session.Session().region_name   -> None
boto3.client('bedrock-runtime')       -> NoRegionError: You must specify a region.
```

**Set `REBAR_LLM_BEDROCK_REGION=us-east-1`** on the bot service. Prefer rebar's own knob over a bare
`AWS_REGION` so the value is visible to rebar's config layer and is recorded in the verdict's
`provider_provenance` (ticket 343b) — an `AWS_REGION` set outside rebar authenticates fine but leaves
no trace in the signed artefact explaining which region produced the verdict.

### IMDS reachability does NOT supply a region

These are **independent** concerns and it is easy to assume otherwise. IMDS is reachable from the
container — the token `PUT` returns 200 and the instance role resolves (see ticket 9249) — and
`session.region_name` is STILL `None`. So:

- fixing an IMDS hop-limit problem does **not** supply a region;
- a working instance role does **not** remove the need to configure one;
- credential discovery and region discovery fail, and are fixed, separately.

Treat the hop-limit contingency and the region setting as two unrelated items on the cutover
checklist, never as one.

Since ticket a574, a missing region no longer surfaces as a bare boto3 `NoRegionError` from deep
inside provider construction: `build_bedrock_provider` pre-checks boto3's own resolution and raises a
typed `LLMConfigError` naming `REBAR_LLM_BEDROCK_REGION`. rebar deliberately does **not** invent a
default region — a wrong region is a silent-until-call misconfiguration, not a value to guess.
