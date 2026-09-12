# Bedrock daily spend cap

A hard ceiling of **$500 per UTC day** on Amazon Bedrock spend in account
`896586841071`. When the day's spend reaches the cap, every principal that can
invoke Bedrock has a deny policy attached. It lifts automatically at 00:00 UTC.

Defined in [`../bedrock_spend_cap.tf`](../bedrock_spend_cap.tf); metering logic
in [`handler.py`](handler.py).

## Why this isn't an AWS Budget action

Because AWS won't allow it. The natural implementation — a DAILY budget with an
`APPLY_IAM_POLICY` action — is rejected by the Budgets API:

```
InvalidParameterException: AWS Budgets Actions don't support daily
granularity budget for now.
```

MONTHLY is the finest period that can auto-apply a deny, and a monthly budget
can't express "$500 today": one runaway day inside the month passes untouched.
There is no native AWS mechanism for a hard per-day dollar cap on Bedrock.

The `bedrock-daily-500` budget still exists, but it only **alerts** (50/80/100%
to the `bedrock-spend-cap-alerts` SNS topic). The Lambda does the enforcing.

## How spend is measured

Two signals, because neither is sufficient alone:

| Signal | Latency | Role |
|---|---|---|
| Cost Explorer | 8–24 h | Authoritative dollars. Catches anything unmetered (provisioned throughput, batch, models publishing no token metrics). |
| CloudWatch `AWS/Bedrock` token counts | ~5 min | Fast tripwire. Priced locally into dollars. |

Enforcement runs on `max(cost_explorer, priced_tokens)`, evaluated every 5
minutes. Five minutes is therefore the enforcement granularity: it bounds how
much a runaway can spend between breaching the cap and being denied.

**Prices are derived from this account's own bills**, not a hardcoded table.
Cost Explorer grouped by `USAGE_TYPE` returns both cost and usage quantity, so
`cost / quantity` is the exact blended $/1K-token rate actually charged — after
whatever region, tier, and discount apply. That rate table refreshes daily and
cannot go stale. (The AWS Price List API was evaluated and rejected: its `model`
dimension for `AmazonBedrock` still tops out at Claude 3, and no `usagetype`
mentions `anthropic`.)

Models with no billing history fall back, in order:

1. a published seed rate (`local.bedrock_cap_seed_rates`) — currently the Claude
   family, which has never been billed on this account;
2. the model's observed input rate scaled by direction (output ×5, cache read
   ×0.1, cache write ×1.25);
3. `UNKNOWN_RATE_PER_1K` = $75/1M tokens, set above any real Bedrock rate so an
   unpriced model **fails closed** — tripping early rather than slipping under.

Cost Explorer bills $0.01 per request, so it is polled hourly rather than every
tick (every-tick polling would cost ~$172/mo). It is always re-read once the
metered estimate passes 80% of the cap, which is exactly when a stale number is
most expensive to trust.

## Operating it

**Check current state:**

```sh
aws lambda invoke --function-name bedrock-spend-cap --profile frontier \
  --region us-east-1 --payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout
```

**Is the cap currently tripped?**

```sh
aws iam list-entities-for-policy --profile frontier \
  --policy-arn arn:aws:iam::896586841071:policy/BedrockDailySpendCapDeny
```

Empty lists mean nothing is denied.

**Lift the cap early** (it otherwise lifts at 00:00 UTC) — detach the policy
from whatever the command above lists, e.g.:

```sh
aws iam detach-role-policy --profile frontier --role-name lmn-api \
  --policy-arn arn:aws:iam::896586841071:policy/BedrockDailySpendCapDeny
```

The Lambda will not re-attach within the same UTC day once `tripped_day` is set,
unless spend is still over the cap at the next tick — which it will be, since
spend only goes up. To genuinely lift for the rest of the day, raise
`THRESHOLD_USD` or set `DRY_RUN=true` on the function, and remember to revert.

**Change the cap:** set `bedrock_daily_cap_usd` and apply. Note that lowering it
below normal daily spend will deny production Bedrock calls during ordinary
operation.

## Keeping it honest

The cap only covers principals listed in `bedrock_cap_target_roles` /
`bedrock_cap_target_groups`. **A role granted Bedrock access but missing from
that list is a hole in the cap.** Re-derive the list when Bedrock permissions
are granted to a new role:

```sh
for r in $(aws iam list-roles --profile frontier \
    --query 'Roles[?!contains(Path,`aws-service-role`)].RoleName' --output text); do
  for p in $(aws iam list-role-policies --profile frontier --role-name "$r" \
      --query 'PolicyNames[]' --output text); do
    aws iam get-role-policy --profile frontier --role-name "$r" --policy-name "$p" \
      --output json | grep -qi 'bedrock:Invoke\|bedrock:Converse' && echo "$r ($p)"
  done
done
```

`BedrockInvocationLoggingRole` is deliberately excluded: it spends nothing, and
denying it would blind the audit trail when it matters most.

## Verifying enforcement still works

Don't test against production roles. Create a throwaway role, point a local run
of the handler at it with `THRESHOLD_USD=0` to force a trip, confirm the deny
attaches, clear the state parameter to simulate a new UTC day, confirm it
detaches, then delete the role. This was the acceptance test for the original
deployment and it passed on all four checks.

The Lambda's IAM permissions are also worth re-checking after any edit — the
`iam:PolicyARN` condition is what stops this role from being a
privilege-escalation path:

```sh
# expect: allowed
aws iam simulate-principal-policy --profile frontier \
  --policy-source-arn arn:aws:iam::896586841071:role/bedrock-spend-cap-lambda \
  --action-names iam:AttachRolePolicy \
  --resource-arns arn:aws:iam::896586841071:role/lmn-api \
  --context-entries 'ContextKeyName=iam:PolicyARN,ContextKeyValues=arn:aws:iam::896586841071:policy/BedrockDailySpendCapDeny,ContextKeyType=string'

# expect: implicitDeny
aws iam simulate-principal-policy --profile frontier \
  --policy-source-arn arn:aws:iam::896586841071:role/bedrock-spend-cap-lambda \
  --action-names iam:AttachRolePolicy \
  --resource-arns arn:aws:iam::896586841071:role/lmn-api \
  --context-entries 'ContextKeyName=iam:PolicyARN,ContextKeyValues=arn:aws:iam::aws:policy/AdministratorAccess,ContextKeyType=string'
```

## Known limits

- **Not instantaneous.** Worst case, a runaway spends for ~5 minutes past the
  cap before the deny lands. A hard real-time cap is not achievable with the
  signals AWS exposes.
- **An in-flight request is not killed.** The deny blocks new invocations; calls
  already accepted by Bedrock complete and bill.
- **SNS has no subscribers yet.** The topic exists and receives alerts, but
  nothing is listening until someone subscribes (subscriptions are not managed
  in Terraform because every useful endpoint needs an out-of-band confirmation
  click, which would otherwise sit pending while reporting success).
