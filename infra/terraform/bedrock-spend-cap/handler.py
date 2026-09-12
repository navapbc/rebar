"""
bedrock_spend_cap — enforces a hard per-UTC-day dollar cap on Amazon Bedrock.

WHY THIS EXISTS AT ALL
----------------------
AWS Budgets Actions cannot drive a daily cap. The Budgets API flatly rejects an
action attached to a DAILY-period budget:

    InvalidParameterException: AWS Budgets Actions don't support daily
    granularity budget for now.

MONTHLY is the finest period that can auto-apply a deny, and a monthly budget
cannot express "$500 today" — one runaway day inside the month would sail
through. So the daily cap has to be enforced outside Budgets. That is this
Lambda. The companion DAILY budget still exists, but only to *alert*.

TWO SIGNALS, BECAUSE NEITHER IS SUFFICIENT ALONE
------------------------------------------------
  * Cost Explorer is authoritative — real billed dollars — but it lags 8-24h.
    On its own it would let a runaway burn far past the cap before firing.
  * CloudWatch AWS/Bedrock token metrics land within ~5 minutes, but they are
    token counts, not dollars.

So we price the CloudWatch tokens ourselves and enforce on
max(cost_explorer, priced_tokens). CE catches anything the metering misses
(provisioned throughput, batch, models that publish no token metrics); the
metering catches a runaway hours before CE would.

WHERE THE PRICES COME FROM
--------------------------
Not a hardcoded table. Cost Explorer grouped by USAGE_TYPE returns BOTH
UnblendedCost and UsageQuantity, so cost/quantity is the exact blended $/unit
this account is actually charged — after whatever region, tier, and discount
apply. That is strictly better than a price list: it cannot go stale, and it is
right for this account rather than right for the public rate card. (The AWS
Price List API was evaluated and rejected: its `model` dimension for
AmazonBedrock still tops out at Claude 3, and no usagetype mentions anthropic.)

Usage types look like `USE1-deepseek.v3.2-input-tokens`, and UsageQuantity is
in units of 1,000 tokens.

A model that has never been billed in the rate window has no empirical rate.
Its tokens are priced at UNKNOWN_RATE_PER_1K, which is set deliberately HIGH so
an unpriced model trips the cap early rather than slipping under it. Failing
closed is the entire point of a cap. Unmatched model ids are logged loudly so
they can be pinned explicitly via RATE_OVERRIDES.

WHY CE IS NOT POLLED EVERY TICK
-------------------------------
Cost Explorer bills $0.01 per API request. At a 5-minute tick that is ~$172/mo
— an absurd way to run a cost control. CE is therefore polled at most once per
CE_MIN_INTERVAL_SECONDS (default hourly) and the rate table is refreshed at
most once a day. The 5-minute tick runs on CloudWatch alone, which is the
signal that actually needs to be fast.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

THRESHOLD_USD = float(os.environ.get("THRESHOLD_USD", "500"))
DENY_POLICY_ARN = os.environ["DENY_POLICY_ARN"]
TARGET_ROLES = [r for r in os.environ.get("TARGET_ROLES", "").split(",") if r]
TARGET_GROUPS = [g for g in os.environ.get("TARGET_GROUPS", "").split(",") if g]
REGIONS = [r for r in os.environ.get("REGIONS", "us-east-1").split(",") if r]
STATE_PARAM = os.environ["STATE_PARAM"]
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
CE_MIN_INTERVAL_SECONDS = int(os.environ.get("CE_MIN_INTERVAL_SECONDS", "3600"))
RATE_WINDOW_DAYS = int(os.environ.get("RATE_WINDOW_DAYS", "60"))
UNKNOWN_RATE_PER_1K = float(os.environ.get("UNKNOWN_RATE_PER_1K", "0.075"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Explicit ModelId-substring -> usage-type-fragment pins, for the handful whose
# billing name and metric name do not normalise to each other (Titan embeddings
# bill as "TitanEmbeddingV2-Text" but meter as "amazon.titan-embed-text-v2:0").
RATE_OVERRIDES = json.loads(os.environ.get("RATE_OVERRIDES", "{}"))

# Published $/1K-token rates for models this account is entitled to but has
# never actually been billed for, so no empirical rate can exist yet. Without
# these, a first-ever Claude run would be priced at UNKNOWN_RATE_PER_1K and trip
# the cap at a small fraction of the real threshold. The moment a model appears
# on a bill its empirical rate wins and the seed stops being consulted.
#
# Seeded at the REGIONAL rate (Bedrock prices regional endpoints ~10% above
# global) so the seed errs high rather than low.
SEED_RATES = json.loads(os.environ.get("SEED_RATES", "{}"))

# CloudWatch token metric -> (candidate usage-type suffixes that bill it, the
# multiplier to apply to the model's input rate when none of those suffixes has
# an observed rate of its own).
#
# The output multiplier is 5.0, not 1.0: across every rate observed on this
# account output bills at 3-5x input, so deriving output from input at parity
# would UNDER-price a runaway and let the cap fire late. Erring high is the
# correct direction for a cap. Cache read/write use Bedrock's published 0.1x /
# 1.25x factors.
TOKEN_METRICS = {
    "InputTokenCount": (("input-tokens", "input-token-count"), 1.0),
    "OutputTokenCount": (("output-tokens", "output-token-count"), 5.0),
    "CacheReadInputTokenCount": (
        ("cache-read-input-token-count", "cache-read-input-tokens"), 0.10),
    "CacheWriteInputTokenCount": (
        ("cache-write-input-token-count", "cache-write-input-tokens"), 1.25),
}

iam = boto3.client("iam")
ssm = boto3.client("ssm")
ce = boto3.client("ce", region_name="us-east-1")
sns = boto3.client("sns") if SNS_TOPIC_ARN else None


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        raw = ssm.get_parameter(Name=STATE_PARAM)["Parameter"]["Value"]
        return json.loads(raw)
    except ssm.exceptions.ParameterNotFound:
        return {}
    except (ClientError, ValueError) as exc:
        # A corrupt or unreadable state parameter must not wedge enforcement.
        # Starting from empty costs one extra CE call, nothing more.
        log.warning("state unreadable (%s); starting empty", exc)
        return {}


def _save_state(state: dict) -> None:
    ssm.put_parameter(
        Name=STATE_PARAM, Value=json.dumps(state), Type="String", Overwrite=True
    )


# --------------------------------------------------------------------------
# empirical rates
# --------------------------------------------------------------------------
def _normalise(text: str) -> str:
    """Collapse a model id or usage-type fragment to comparable alphanumerics."""
    text = text.lower()
    prefixes = ("us.", "eu.", "apac.", "global.", "anthropic.", "amazon.",
                "openai.", "mistral.", "deepseek.", "cohere.", "meta.", "ai21.")
    # Loop, not a single pass: ids stack them ("us.anthropic.claude-sonnet-4-6",
    # "us.amazon.nova-lite-v1:0") and stripping only the first leaves a vendor
    # name embedded in the comparison key.
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix):]
                changed = True
    return re.sub(r"[^a-z0-9]", "", text)


def _refresh_rates() -> dict:
    """Derive $/1K-tokens per usage type from this account's own bills."""
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=RATE_WINDOW_DAYS)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )
    totals: dict[str, list[float]] = {}
    for period in resp.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            usage_type = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            qty = float(group["Metrics"]["UsageQuantity"]["Amount"])
            acc = totals.setdefault(usage_type, [0.0, 0.0])
            acc[0] += cost
            acc[1] += qty

    rates = {
        usage_type: cost / qty
        for usage_type, (cost, qty) in totals.items()
        if qty > 0 and cost > 0
    }
    log.info("refreshed %d empirical rates", len(rates))
    return rates


def _seed_for(model_id: str, metric_name: str) -> float | None:
    """Published $/1K rate for a model with no billing history yet."""
    for needle, prices in SEED_RATES.items():
        if needle not in model_id:
            continue
        if metric_name == "OutputTokenCount":
            return prices.get("output")
        base = prices.get("input")
        if base is None:
            return None
        if metric_name == "CacheReadInputTokenCount":
            return base * 0.10
        if metric_name == "CacheWriteInputTokenCount":
            return base * 1.25
        return base
    return None


def _rate_for(model_id: str, metric_name: str, rates: dict) -> float:
    """Best $/1K-token rate for one model and token direction.

    Resolution order, most trustworthy first:
      1. an observed rate for this exact model AND token direction
      2. the model's published seed rate
      3. the model's observed INPUT rate scaled by the direction multiplier
      4. UNKNOWN_RATE_PER_1K, which is set high so unpriced models fail closed
    """
    suffixes, mult = TOKEN_METRICS[metric_name]
    pin = None
    for needle, fragment in RATE_OVERRIDES.items():
        if needle in model_id:
            pin = _normalise(fragment)
            break
    target = pin or _normalise(model_id)

    def _match(want_suffixes: tuple[str, ...]) -> float | None:
        best = None
        for usage_type, rate in rates.items():
            hit = next((s for s in want_suffixes if usage_type.endswith(s)), None)
            if hit is None:
                continue
            # Strip the "USE1-" style region prefix and the direction suffix.
            body = usage_type[: -len(hit)].rstrip("-")
            body = body.split("-", 1)[1] if "-" in body else body
            norm = _normalise(body)
            if not norm:
                continue
            if norm == target or norm in target or target in norm:
                # Prefer the most specific (longest) matching fragment.
                if best is None or len(norm) > best[0]:
                    best = (len(norm), rate)
        return best[1] if best else None

    exact = _match(suffixes)
    if exact is not None:
        return exact

    seed = _seed_for(model_id, metric_name)
    if seed is not None:
        return seed

    base = _match(TOKEN_METRICS["InputTokenCount"][0])
    if base is not None:
        return base * mult

    log.warning("no rate for model=%s metric=%s; using fail-closed fallback",
                model_id, metric_name)
    return UNKNOWN_RATE_PER_1K * mult


# --------------------------------------------------------------------------
# metered spend (fast signal)
# --------------------------------------------------------------------------
def _metered_spend(day_start: dt.datetime, now: dt.datetime, rates: dict) -> float:
    total = 0.0
    for region in REGIONS:
        cw = boto3.client("cloudwatch", region_name=region)
        queries, meta = [], {}
        paginator = cw.get_paginator("list_metrics")
        for page in paginator.paginate(Namespace="AWS/Bedrock"):
            for metric in page.get("Metrics", []):
                name = metric["MetricName"]
                if name not in TOKEN_METRICS:
                    continue
                dims = metric.get("Dimensions", [])
                # Only the per-ModelId series; the undimensioned aggregate would
                # double-count everything already covered below.
                if len(dims) != 1 or dims[0]["Name"] != "ModelId":
                    continue
                qid = f"q{len(queries)}"
                meta[qid] = (dims[0]["Value"], name)
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {"Namespace": "AWS/Bedrock",
                                   "MetricName": name,
                                   "Dimensions": dims},
                        "Period": 3600,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                })

        for batch_start in range(0, len(queries), 100):
            batch = queries[batch_start:batch_start + 100]
            if not batch:
                continue
            resp = cw.get_metric_data(
                MetricDataQueries=batch, StartTime=day_start, EndTime=now,
                ScanBy="TimestampAscending",
            )
            for result in resp.get("MetricDataResults", []):
                units_1k = sum(result.get("Values", []) or []) / 1000.0
                if units_1k <= 0:
                    continue
                model_id, metric_name = meta[result["Id"]]
                total += units_1k * _rate_for(model_id, metric_name, rates)
    return total


# --------------------------------------------------------------------------
# authoritative spend (slow signal)
# --------------------------------------------------------------------------
def _ce_spend_today(day: dt.date) -> float:
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": day.isoformat(),
                    "End": (day + dt.timedelta(days=1)).isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
    )
    results = resp.get("ResultsByTime", [])
    if not results:
        return 0.0
    return float(results[0]["Total"]["UnblendedCost"]["Amount"])


# --------------------------------------------------------------------------
# enforcement
# --------------------------------------------------------------------------
def _is_attached() -> bool:
    """Ask IAM, never the cached state — the live attachment is the truth."""
    for role in TARGET_ROLES:
        try:
            attached = iam.list_attached_role_policies(RoleName=role)
            if any(p["PolicyArn"] == DENY_POLICY_ARN
                   for p in attached["AttachedPolicies"]):
                return True
        except iam.exceptions.NoSuchEntityException:
            continue
    for group in TARGET_GROUPS:
        try:
            attached = iam.list_attached_group_policies(GroupName=group)
            if any(p["PolicyArn"] == DENY_POLICY_ARN
                   for p in attached["AttachedPolicies"]):
                return True
        except iam.exceptions.NoSuchEntityException:
            continue
    return False


def _apply(attach: bool) -> list[str]:
    """Attach or detach the deny on every target. Best-effort per principal:
    one failure must not abort the rest of the sweep."""
    touched = []
    if DRY_RUN:
        log.warning("DRY_RUN set; would %s %d principals",
                    "attach" if attach else "detach",
                    len(TARGET_ROLES) + len(TARGET_GROUPS))
        return touched

    for role in TARGET_ROLES:
        try:
            if attach:
                iam.attach_role_policy(RoleName=role, PolicyArn=DENY_POLICY_ARN)
            else:
                iam.detach_role_policy(RoleName=role, PolicyArn=DENY_POLICY_ARN)
            touched.append(f"role/{role}")
        except ClientError as exc:
            log.error("failed to %s role %s: %s",
                      "attach" if attach else "detach", role, exc)
    for group in TARGET_GROUPS:
        try:
            if attach:
                iam.attach_group_policy(GroupName=group, PolicyArn=DENY_POLICY_ARN)
            else:
                iam.detach_group_policy(GroupName=group, PolicyArn=DENY_POLICY_ARN)
            touched.append(f"group/{group}")
        except ClientError as exc:
            log.error("failed to %s group %s: %s",
                      "attach" if attach else "detach", group, exc)
    return touched


def _notify(subject: str, message: str) -> None:
    if not sns:
        return
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
    except ClientError as exc:
        log.error("SNS publish failed: %s", exc)


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------
def handler(event, context):  # noqa: ARG001 - Lambda signature
    now = dt.datetime.now(dt.timezone.utc)
    day = now.date()
    day_start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    state = _load_state()

    # --- rates: refresh at most daily (one CE call) ------------------------
    rates = state.get("rates") or {}
    if state.get("rates_day") != day.isoformat() or not rates:
        try:
            rates = _refresh_rates()
            state["rates"] = rates
            state["rates_day"] = day.isoformat()
        except ClientError as exc:
            log.error("rate refresh failed, reusing cached rates: %s", exc)

    # --- fast signal: every tick, CloudWatch only --------------------------
    metered = _metered_spend(day_start, now, rates)

    # --- slow signal: CE, rate-limited to protect against its $0.01/call ----
    ce_spend = float(state.get("ce_spend", 0.0)) if state.get("ce_day") == day.isoformat() else 0.0
    last_poll = state.get("ce_last_poll")
    due = (
        state.get("ce_day") != day.isoformat()
        or not last_poll
        or (now - dt.datetime.fromisoformat(last_poll)).total_seconds() >= CE_MIN_INTERVAL_SECONDS
    )
    # Always take a fresh authoritative read when the meter says we are close;
    # that is exactly the moment a stale CE number is most expensive to trust.
    if due or metered >= THRESHOLD_USD * 0.8:
        try:
            ce_spend = _ce_spend_today(day)
            state["ce_spend"] = ce_spend
            state["ce_day"] = day.isoformat()
            state["ce_last_poll"] = now.isoformat()
        except ClientError as exc:
            log.error("CE poll failed, using cached value: %s", exc)

    spend = max(ce_spend, metered)
    attached = _is_attached()
    log.info(
        "day=%s metered=%.4f ce=%.4f effective=%.4f threshold=%.2f attached=%s",
        day, metered, ce_spend, spend, THRESHOLD_USD, attached,
    )

    # --- enforce -----------------------------------------------------------
    if spend >= THRESHOLD_USD and not attached:
        touched = _apply(attach=True)
        state["tripped_day"] = day.isoformat()
        _notify(
            f"Bedrock daily cap TRIPPED — ${spend:,.2f} >= ${THRESHOLD_USD:,.2f}",
            f"Bedrock spend for {day} (UTC) reached ${spend:,.2f}.\n"
            f"  metered (CloudWatch, ~5 min lag): ${metered:,.2f}\n"
            f"  billed  (Cost Explorer, 8-24h lag): ${ce_spend:,.2f}\n\n"
            f"Deny policy {DENY_POLICY_ARN} attached to:\n  "
            + "\n  ".join(touched or ["(nothing — see logs)"])
            + "\n\nThis lifts automatically at 00:00 UTC. To lift it sooner, detach "
              "the policy from the principals above.",
        )
    elif attached and spend < THRESHOLD_USD and state.get("tripped_day") != day.isoformat():
        # New UTC day and back under the cap — release.
        touched = _apply(attach=False)
        _notify(
            "Bedrock daily cap released",
            f"New UTC day ({day}); Bedrock spend is ${spend:,.2f}, under the "
            f"${THRESHOLD_USD:,.2f} cap. Deny policy detached from:\n  "
            + "\n  ".join(touched or ["(nothing — see logs)"]),
        )

    _save_state(state)
    return {
        "day": day.isoformat(),
        "metered_usd": round(metered, 4),
        "ce_usd": round(ce_spend, 4),
        "effective_usd": round(spend, 4),
        "threshold_usd": THRESHOLD_USD,
        "deny_attached": _is_attached(),
    }
