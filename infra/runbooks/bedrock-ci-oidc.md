# Runbook — the Bedrock CI role for the external suite's provider matrix (operator steps)

The external suite's live-LLM lane runs one arm per provider
(`.github/workflows/external-integration.yml`, job `external-llm`; design + cost in
[docs/ci-provider-matrix.md](../../docs/ci-provider-matrix.md)). The **Bedrock** arm needs AWS
credentials on a **GitHub-hosted `ubuntu-latest` runner**, and this runbook is the definition of
the IAM role it assumes.

> **This role NOW EXISTS**, created by an operator on 2026-08-03 exactly as specified below:
> `arn:aws:iam::896586841071:role/rebar-external-ci-bedrock`, with inline policy
> `rebar-external-ci-bedrock-converse` and no managed policies attached. Both repository
> variables are set (`AWS_BEDROCK_CI_ROLE_ARN`, `AWS_BEDROCK_CI_REGION=us-east-1`).
> Nothing in this repository creates it — it is operator-owned, and this file remains its
> definition of record. Verify the live state against the JSON below with the commands in
> §"Verify"; if they ever diverge, the JSON here is the intent and the live role is the drift.
>
> Were the role or either variable absent, the Bedrock arm **fails its preflight step with an
> `::error::`** rather than skipping — deliberately, so an unconfigured arm can never be mistaken
> for a passing one.

---

## Why not the S7 instance role

Story S7 grants the review bot Bedrock access through the **EC2 instance role**
`rebar-gerrit-instance-role`, reached over **IMDS** from inside `compose-review-bot-1`
(`infra/terraform/iam_s7.tf`, `infra/runbooks/bedrock-access.md`). That path **does not exist**
from a GitHub-hosted runner: there is no instance role on the runner and no IMDS route to the
Gerrit host. An earlier draft of this work assumed the precedent transferred; it does not.

What transfers is the **permission policy shape**, not the delivery path. So: a dedicated role,
assumed by **OIDC web-identity federation**, carrying the same Claude-only Bedrock grant.

The rejected alternative is a long-lived `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` pair in a
repository secret — a durable AWS credential parked in CI to save a one-time role setup.
`tests/unit/test_ci_provider_matrix.py::test_no_static_aws_keys_are_configured_anywhere` fails if
one ever appears.

---

## Prerequisites that already exist

- **The GitHub Actions OIDC identity provider**, account-wide and rebar-owned
  (`infra/terraform/oidc.tf`):
  `arn:aws:iam::896586841071:oidc-provider/token.actions.githubusercontent.com`
  (verified present with `aws iam list-open-id-connect-providers`). Do **not** create a second
  one — there is exactly one per account, keyed on the URL.
- **The precedent role** `rebar-terraform-plan` (`infra/terraform/iam.tf`), which federates the
  same provider from `.github/workflows/terraform-drift.yml`. Its trust policy is the model for
  the one below.

---

## The role to create

**Role name:** `rebar-external-ci-bedrock`

### Trust policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GitHubActionsMainOfThisRepoOnly",
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::896586841071:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:repository": "navapbc/rebar",
          "token.actions.githubusercontent.com:sub": "repo:navapbc/rebar:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

**Read the conditions as three separate locks, because each closes a different hole:**

- `aud = sts.amazonaws.com` — pins the audience the OIDC provider was registered with. Without
  it a token minted for a different audience could be replayed.
- `repository = navapbc/rebar` — pins the repository by name. **A trust policy without a
  repository or `sub` scope is assumable by any GitHub Actions workflow in the world**; that is
  the classic and serious misconfiguration here, not a theoretical one.
- `sub` EQUAL TO `repo:navapbc/rebar:ref:refs/heads/main` — pins the *context* to **main of this
  repository, and nothing else**. Note it sits under `StringEquals`, not `StringLike`: with no
  wildcard needed there is no glob to reason about, which is strictly stronger. This excludes a
  `pull_request` context, whose subject is `repo:navapbc/rebar:pull_request`, and it also excludes
  every non-main branch. A bare `repo:navapbc/rebar:*` (the shape
  `rebar-terraform-plan` uses) does **not** exclude that, so do not copy it verbatim here.

**Why main-only, and why it costs nothing here.** An earlier draft of this runbook used
`refs/heads/*` on the reasoning that an operator validating a change would dispatch the suite from
that change's branch. That is NOT how this repository works, and the operator said so directly:
the external integration suite is not run on branches, and pull requests on the GitHub mirror are
closed with a redirect to Gerrit. So no non-main ref ever has a legitimate need for this role, and
the wildcard bought nothing while widening the trust boundary. Pinning `sub` to main exactly
removes that.

What this closes that `refs/heads/*` did not: anyone able to push a branch to the mirror could
have run a workflow assuming this role. Now only a run whose ref IS `refs/heads/main` can.
Combined with the permission policy below, the worst case was already bounded to Bedrock Claude
token spend rather than data access — but "bounded" is not a reason to leave a door open.

One tighter variant remains available:

| Variant | `sub` condition | What it additionally closes | Cost |
|---|---|---|---|
| environment-scoped | `repo:navapbc/rebar:environment:bedrock-ci` | adds required-reviewer gating on top of main-only | needs a GitHub environment named `bedrock-ci` **and** `environment: bedrock-ci` added to the `external-llm` job; the arm fails until both exist |

Note the consequence for dispatching, so it is not a surprise: `workflow_dispatch` must be run
against **main**. Dispatching from any other ref makes the Bedrock arm fail role assumption —
loudly, at the `configure-aws-credentials` step, which is the intended behaviour rather than a
silent skip.

### Permission policy

Inline policy name: `rebar-external-ci-bedrock-converse`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ConverseAnthropicClaudeOnly",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*:896586841071:inference-profile/*.anthropic.claude-*"
      ]
    }
  ]
}
```

This is `iam_s7.tf`'s `aws_iam_policy_document.bedrock_converse`, verbatim in shape. Its
non-obvious parts are load-bearing and were each got wrong once — do not "simplify" them:

- **The action is `bedrock:InvokeModel`, not `bedrock:Converse`.** rebar calls the Bedrock
  **Converse** API (via pydantic-ai's `BedrockProvider`), and Converse **authorizes against
  `InvokeModel`**. `bedrock:Converse` exists as a distinct, *simulatable* action, so a policy
  simulator will report it denied — which reads as proof you must grant it. That inference is
  wrong; see `infra/runbooks/bedrock-access.md`.
- **Both resource shapes are required** for a cross-region inference profile: the profile ARN
  *and* the underlying foundation models in every region the profile can route to.
- **The profile prefix is wildcarded (`*.anthropic.claude-*`), not pinned to `us.`** — `global.`
  profiles are a valid, measured form in this account, and pinning `us.` would deny a legitimate
  model id at runtime.
- **The region field is wildcarded; the MODEL is not.** Claude only — a call to any other
  vendor's model is denied, which is the least-privilege oracle.

`bedrock:ListInferenceProfiles` is deliberately **not** granted: the suite only invokes. Discover
new profile ids from an operator identity instead.

### Create it

```sh
ACCOUNT=896586841071
# Write the two documents above to trust.json / permissions.json first.
aws iam create-role --role-name rebar-external-ci-bedrock \
  --assume-role-policy-document file://trust.json \
  --tags Key=Project,Value=rebar Key=Ticket,Value=f124

aws iam put-role-policy --role-name rebar-external-ci-bedrock \
  --policy-name rebar-external-ci-bedrock-converse \
  --policy-document file://permissions.json

aws iam get-role --role-name rebar-external-ci-bedrock \
  --query 'Role.Arn' --output text
```

### Terraform: intentionally NOT committed here

`infra/terraform/` is guarded by `.github/workflows/terraform-drift.yml`, which runs
`terraform plan -detailed-exitcode` and **fails on a non-empty plan** — that is, on any committed
HCL whose state does not already match reality. Committing an `iam_ci_bedrock.tf` would turn the
drift check **red** the moment it merged, for every change, until someone reconciled it.

That is still true now that the role exists, for a different reason: the role was created with the
AWS CLI, so terraform has **no state entry** for it. Committed HCL would therefore plan a CREATE of
a role that already exists — which fails on a name collision rather than converging. Adopting it
into terraform requires `terraform import` (or an equivalent `plan`-clean apply) **in the same
change as the HCL**. So the definition lives here as JSON, and the equivalent HCL is below for
whoever does that adoption:

```hcl
# infra/terraform/iam_f124.tf — add ONLY together with an apply (see the drift-gate note above).
# SINGLE-OWNER: story f124 owns the external-suite Bedrock CI role. Do NOT edit iam.tf (owned by
# S1) or iam_s7.tf (owned by S7); this follows the iam_s2.tf / iam_s4a.tf pattern of a
# separately-named file attaching its own scoped policy to its own role.
data "aws_iam_policy_document" "gha_external_ci_bedrock_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:repository"
      values   = ["navapbc/rebar"]
    }
    # main ONLY. Exact match, not StringLike: the suite is never run on a branch, and mirror
    # PRs are closed with a redirect to Gerrit, so no other ref needs this role.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:navapbc/rebar:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "gha_external_ci_bedrock" {
  name               = "rebar-external-ci-bedrock"
  assume_role_policy = data.aws_iam_policy_document.gha_external_ci_bedrock_assume.json

  tags = {
    Project = "rebar"
    Ticket  = "f124"
  }
}

data "aws_iam_policy_document" "external_ci_bedrock_converse" {
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

resource "aws_iam_role_policy" "external_ci_bedrock_converse" {
  name   = "rebar-external-ci-bedrock-converse"
  role   = aws_iam_role.gha_external_ci_bedrock.id
  policy = data.aws_iam_policy_document.external_ci_bedrock_converse.json
}
```

---

## Wire it to the workflow — two repository VARIABLES

Neither value is a secret (a role ARN and a region are not credentials), so both are repository
**variables**. The Bedrock arm's preflight requires both and fails loudly naming whichever is
missing.

```sh
gh variable set AWS_BEDROCK_CI_ROLE_ARN \
  --body "arn:aws:iam::896586841071:role/rebar-external-ci-bedrock"
gh variable set AWS_BEDROCK_CI_REGION --body "us-east-1"
```

`AWS_BEDROCK_CI_REGION` feeds **three** things, on purpose: `configure-aws-credentials`'
`aws-region`, and **both** `AWS_DEFAULT_REGION` and `REBAR_LLM_BEDROCK_REGION` on the suite step.

**A region is neither optional nor discoverable.** MEASURED on ticket a574: IMDS supplies **no**
region (`boto3.session.Session().region_name is None` on a host with a working instance role), and
rebar's own `REBAR_LLM_BEDROCK_REGION` **alone was insufficient** — `AWS_DEFAULT_REGION` was
required as well. Credential discovery and region discovery fail, and are fixed, separately; do
not treat them as one checklist item. With neither resolving, `build_bedrock_provider` raises a
typed `LLMConfigError` naming the knob rather than a bare boto3 `NoRegionError`
(`tests/unit/test_bedrock_provider.py::test_missing_region_raises_a_typed_error_naming_the_setting`).

---

## Verify

1. **Policy layer** — simulate against the ROLE, not your own identity (an operator holding
   `bedrock:*` cannot fail, so its success carries zero information):

   ```sh
   aws iam simulate-principal-policy \
     --policy-source-arn arn:aws:iam::896586841071:role/rebar-external-ci-bedrock \
     --action-names bedrock:InvokeModel bedrock:InvokeModelWithResponseStream \
     --resource-arns arn:aws:bedrock:us-east-1:896586841071:inference-profile/us.anthropic.claude-sonnet-4-6 \
     --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text
   ```

   Expect `allowed` for both. **This is necessary and not sufficient** — it answers "would the
   policy permit the action I named", not "which action does the service check".

2. **Negative control** — repeat with `--resource-arns
   arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0`. Expect `implicitDeny`. Without
   this you have shown the role can call *something*, not that the Claude-only scope binds.

3. **Runtime layer — the only one that settles it.** Dispatch the workflow and read the Bedrock
   arm. `tests/external/test_provider_matrix_live.py` asserts, from inside the arm, that all three
   model classes resolved to `bedrock:`, that no Anthropic/OpenAI key is present, and that a
   region resolved; the live tests then make real Converse calls. A green Bedrock arm with the
   `llm-live-canary` reporting `executed>0` is the proof. An arm reporting
   `[llm-live-canary] FAIL` skipped everything and proves nothing.

## See also

- `infra/runbooks/bedrock-access.md` — the review bot's instance-role Bedrock path, the
  `InvokeModel`-vs-`Converse` evidence, and how **not** to verify Bedrock access.
- `docs/ci-provider-matrix.md` — the matrix's design, measured cost, and cadence decision.
