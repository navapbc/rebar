# Bedrock daily spend cap

A hard ceiling on Amazon Bedrock spend in account `896586841071`. When the
day's spend reaches the configured cap, every principal that can invoke Bedrock
has a deny policy attached. It lifts automatically at 00:00 UTC.

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
can't express the configured cap for today: one runaway day inside the month
passes untouched. There is no native AWS mechanism for a hard per-day dollar cap
on Bedrock.

The `bedrock-daily-cap` budget still exists, but it only **alerts** (50/80/100%
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

**Every token is priced at a ceiling rate for its direction**, set at 1.25x the
highest rate this account has ever been billed for that direction. The figure the
cap reports is therefore an **upper bound on real spend, not an estimate of it**,
and the metered arm cannot fire late.

There is no derived rate table and no per-model matching. An earlier design
derived rates from Cost Explorer and matched them to CloudWatch ModelIds by
substring and suffix heuristics; that produced four distinct mispricings in twelve
days, two of which reached production, because AWS bills one model family under
several label conventions. A single ceiling per direction removes the class:
there is nothing left to match.

The cost is precision, in the safe direction. A model much cheaper than the
priciest is over-priced by the ratio between them — on this account the cheapest
models are two orders of magnitude below the ceiling, and a couple bill nothing
at all for cache writes. So **a large embedding or small-model batch job can trip
the cap well below the configured dollar figure.** That is accepted: erring early
is correct for a circuit breaker. If it ever bites in practice, the remedy is an
exact-match `{ModelId: rate}` override table defaulting to the ceiling — equality
only, never substring matching, so it cannot reopen the class above.

In practice the metered arm has run between roughly 1.1x and 1.9x of settled
billing, so the effective cap sits somewhere below the configured figure rather
than on it, varying with the day's model mix. The measured distribution is in the
operator's local-only notes; it is deliberately not reproduced here, because this
repository is public.

`THRESHOLD_USD` is **not** scaled up to compensate. Raising it to centre the
metered arm would raise it for the Cost Explorer arm too, and that arm is real
billed dollars — the authoritative signal would then trip late, which is the
failure this design exists to remove. Unscaled, Cost Explorer trips at exactly
the configured cap and the metered arm trips early.

A daily guard re-prices a settled day and compares it against Cost Explorer for
that same day, warning if the estimate ever falls *below* actual, which is the
upper-bound property being violated. It only warns: it cannot attach or detach
the deny policy, so a guard fault cannot cause an outage.

Cost Explorer charges per request, so it is polled hourly rather than every
tick. It is always re-read once the metered estimate passes 80% of the cap,
which is exactly when a stale number is most expensive to trust.

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

If the measured spend is still over the cap, the Lambda re-attaches on the next
tick. To genuinely lift for the rest of the day, raise the configured threshold
or set `DRY_RUN=true` on the function, and remember to revert.

**Change the cap:** set `bedrock_daily_cap_usd` and apply. Note that lowering it
below normal daily spend will deny production Bedrock calls during ordinary
operation.

## Keeping it honest

The cap only covers principals listed in `bedrock_cap_target_roles` /
`bedrock_cap_target_groups`. **A role granted Bedrock access but missing from
that list is a hole in the cap.** Re-derive the list when Bedrock permissions
are granted to a new role by enumerating non-service roles, reading their inline
policies, and checking for Bedrock invocation actions.

`BedrockInvocationLoggingRole` is deliberately excluded: it spends nothing, and
denying it would blind the audit trail when it matters most.

## Verifying enforcement still works

Don't test against production roles. Create a throwaway role, point a local run
of the handler at it with a threshold value that forces a trip, confirm the deny
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
