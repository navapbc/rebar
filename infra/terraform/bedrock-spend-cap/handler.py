"""
bedrock_spend_cap — enforces a hard per-UTC-day dollar cap on Amazon Bedrock.

WHY THIS EXISTS AT ALL
----------------------
AWS Budgets Actions cannot drive a daily cap. The Budgets API flatly rejects an
action attached to a DAILY-period budget:

    InvalidParameterException: AWS Budgets Actions don't support daily
    granularity budget for now.

MONTHLY is the finest period that can auto-apply a deny, and a monthly budget
cannot express the configured cap for today — one runaway day inside the month
would sail through. So the daily cap has to be enforced outside Budgets. That
is this Lambda. The companion DAILY budget still exists, but only to *alert*.

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
UnblendedCost and UsageQuantity, so cost/quantity is the exact blended
unit rate
this account is actually charged — after whatever region, tier, and discount
apply. That is strictly better than a price list: it cannot go stale, and it is
right for this account rather than right for the public rate card. (The AWS
Price List API was evaluated and rejected: its `model` dimension for
AmazonBedrock still tops out at Claude 3, and no usagetype mentions anthropic.)

Usage types look like the Bedrock model id plus token direction, and UsageQuantity is
in units of 1,000 tokens.

A model that has never been billed in the rate window has no empirical rate.
Its tokens are priced at UNKNOWN_RATE_PER_1K, which is set deliberately HIGH so
an unpriced model trips the cap early rather than slipping under it. Failing
closed is the entire point of a cap. Unmatched model ids are logged loudly so
they can be pinned explicitly via RATE_OVERRIDES.

WHY CE IS NOT POLLED EVERY TICK
-------------------------------
Cost Explorer charges per API request. Polling it on the 5-minute tick is an
inefficient way to run a cost control. CE is therefore polled at most once per
CE_MIN_INTERVAL_SECONDS (default hourly) and the rate table is refreshed at
most once a day. The 5-minute tick runs on CloudWatch alone, which is the signal
that actually needs to be fast.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Literal

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

THRESHOLD_USD = float(os.environ["THRESHOLD_USD"])
WARN_THRESHOLD_USD = float(os.environ["WARN_THRESHOLD_USD"])
DENY_POLICY_ARN = os.environ["DENY_POLICY_ARN"]
TARGET_ROLES = [r for r in os.environ.get("TARGET_ROLES", "").split(",") if r]
TARGET_GROUPS = [g for g in os.environ.get("TARGET_GROUPS", "").split(",") if g]
REGIONS = [r for r in os.environ.get("REGIONS", "us-east-1").split(",") if r]
STATE_PARAM = os.environ["STATE_PARAM"]
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
CE_MIN_INTERVAL_SECONDS = int(os.environ.get("CE_MIN_INTERVAL_SECONDS", "3600"))
RATE_WINDOW_DAYS = int(os.environ.get("RATE_WINDOW_DAYS", "60"))
UNKNOWN_RATE_PER_1K = float(os.environ["UNKNOWN_RATE_PER_1K"])
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Explicit ModelId-substring -> usage-type-fragment pins, for the handful whose
# billing name and metric name do not normalise to each other (Titan embeddings
# bill as "TitanEmbeddingV2-Text" but meter as "amazon.titan-embed-text-v2:0").
RATE_OVERRIDES = json.loads(os.environ.get("RATE_OVERRIDES", "{}"))

# Published per-token rates for models this account is entitled to but has
# never actually been billed for, so no empirical rate can exist yet. Without
# these, a first-ever Claude run would be priced at UNKNOWN_RATE_PER_1K and trip
# the cap at a small fraction of the real threshold. The moment a model appears
# on a bill its empirical rate wins and the seed stops being consulted.
#
# Seeded at the REGIONAL rate (Bedrock prices regional endpoints ~10% above
# global) so the seed errs high rather than low.
SEED_RATES = json.loads(os.environ.get("SEED_RATES", "{}"))

BEDROCK_SERVICE_VALUES = [
    "Amazon Bedrock",
    "Claude Sonnet 4.5 (Amazon Bedrock Edition)",
    "Claude Sonnet 4.6 (Amazon Bedrock Edition)",
    "Claude Haiku 4.5 (Amazon Bedrock Edition)",
    "Claude Opus 4.7 (Amazon Bedrock Edition)",
]
BEDROCK_EDITION_SUFFIX = " (Amazon Bedrock Edition)"
MARKETPLACE_TOKEN_SUFFIXES = {
    "InputTokenCount": "input-tokens",
    "OutputTokenCount": "output-tokens",
    "CacheReadInputTokenCount": "cache-read-input-token-count",
    "CacheWriteInputTokenCount": "cache-write-input-token-count",
}

# CloudWatch token metric -> (candidate usage-type suffixes that bill it, the
# multiplier to apply to the model's input rate when none of those suffixes has
# an observed rate of its own).
#
# The output multiplier is higher than input: across every rate observed on this
# account output bills at several times input, so deriving output from input at parity
# would UNDER-price a runaway and let the cap fire late. Erring high is the
# correct direction for a cap. Cache read/write use Bedrock's published factors.
TOKEN_METRICS = {
    "InputTokenCount": (("input-tokens", "input-token-count"), 1),
    "OutputTokenCount": (("output-tokens", "output-token-count"), 5),
    "CacheReadInputTokenCount": (
        ("cache-read-input-token-count", "cache-read-input-tokens"),
        1 / 10,
    ),
    "CacheWriteInputTokenCount": (
        ("cache-write-input-token-count", "cache-write-input-tokens"),
        5 / 4,
    ),
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
    ssm.put_parameter(Name=STATE_PARAM, Value=json.dumps(state), Type="String", Overwrite=True)


# --------------------------------------------------------------------------
# empirical rates
# --------------------------------------------------------------------------
def _normalise(text: str) -> str:
    """Collapse a model id or usage-type fragment to comparable alphanumerics."""
    text = text.lower()
    prefixes = (
        "us.",
        "eu.",
        "apac.",
        "global.",
        "anthropic.",
        "amazon.",
        "openai.",
        "mistral.",
        "deepseek.",
        "cohere.",
        "meta.",
        "ai21.",
    )
    # Loop, not a single pass: ids stack them ("us.anthropic.claude-sonnet-4-6",
    # "us.amazon.nova-lite-v1:0") and stripping only the first leaves a vendor
    # name embedded in the comparison key.
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix) :]
                changed = True
    return re.sub(r"[^a-z0-9]", "", text)


def _marketplace_rate_key(service: str, usage_type: str) -> tuple[str, float] | None:
    if not service.endswith(BEDROCK_EDITION_SUFFIX):
        return None
    usage_body = usage_type.rsplit(":", 1)[-1]
    try:
        region, token_units = usage_body.split("_", 1)
    except ValueError:
        return None
    if not token_units.endswith("-Units"):
        return None
    token_kind = token_units[: -len("-Units")]
    suffix = MARKETPLACE_TOKEN_SUFFIXES.get(token_kind)
    if suffix is None:
        return None
    model_label = service.removesuffix(BEDROCK_EDITION_SUFFIX)
    return f"{region}-{model_label}-{suffix}", 1000


def _rate_key_for_ce_group(service: str, usage_type: str) -> tuple[str, float] | None:
    if service == "Amazon Bedrock":
        return usage_type, 1
    return _marketplace_rate_key(service, usage_type)


def _ce_cost_and_usage_pages(request: dict):
    next_token = None
    while True:
        page_request = dict(request)
        if next_token:
            page_request["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**page_request)
        yield resp
        next_token = resp.get("NextPageToken")
        if not next_token:
            break


def _refresh_rates() -> dict:
    """Derive per-token rates per usage type from this account's own bills."""
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=RATE_WINDOW_DAYS)
    request = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost", "UsageQuantity"],
        "Filter": {"Dimensions": {"Key": "SERVICE", "Values": BEDROCK_SERVICE_VALUES}},
        "GroupBy": [
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
        ],
    }
    totals: dict[str, list[float]] = {}
    for resp in _ce_cost_and_usage_pages(request):
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                service, usage_type = group["Keys"]
                rate_key = _rate_key_for_ce_group(service, usage_type)
                if rate_key is None:
                    continue
                usage_type, quantity_divisor = rate_key
                cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
                qty = float(group["Metrics"]["UsageQuantity"]["Amount"])
                acc = totals.setdefault(usage_type, [float(0), float(0)])
                acc[0] += cost
                acc[1] += qty / quantity_divisor

    rates = {
        usage_type: cost / qty for usage_type, (cost, qty) in totals.items() if qty > 0 and cost > 0
    }
    log.info("refreshed %d empirical rates", len(rates))
    return rates


def _seed_for(model_id: str, metric_name: str) -> float | None:
    """Published rate for a model with no billing history yet."""
    for needle, prices in SEED_RATES.items():
        if needle not in model_id:
            continue
        if metric_name == "OutputTokenCount":
            return prices.get("output")
        base = prices.get("input")
        if base is None:
            return None
        if metric_name == "CacheReadInputTokenCount":
            return base / 10
        if metric_name == "CacheWriteInputTokenCount":
            return base * 5 / 4
        return base
    return None


RateSource = Literal["empirical", "seed", "empirical_scaled", "fallback"]


@dataclass(frozen=True)
class MeteredRate:
    rate: float
    source: RateSource


def _rate_for(model_id: str, metric_name: str, rates: dict) -> MeteredRate:
    """Best per-token rate for one model and token direction.

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
        return MeteredRate(rate=exact, source="empirical")

    seed = _seed_for(model_id, metric_name)
    if seed is not None:
        return MeteredRate(rate=seed, source="seed")

    base = _match(TOKEN_METRICS["InputTokenCount"][0])
    if base is not None:
        return MeteredRate(rate=base * mult, source="empirical_scaled")

    log.warning("no rate for model=%s metric=%s; using fail-closed fallback", model_id, metric_name)
    return MeteredRate(rate=UNKNOWN_RATE_PER_1K * mult, source="fallback")


# --------------------------------------------------------------------------
# metered spend (fast signal)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MeteredFailure:
    region: str
    exception_class: str


@dataclass(frozen=True)
class MeteredSpend:
    total: float
    metered_estimated: bool
    degraded: bool
    failures: tuple[MeteredFailure, ...]


def _record_metered_failure(
    failures: list[MeteredFailure],
    *,
    region: str,
    operation: str,
    exc: ClientError,
) -> None:
    exception_class = exc.__class__.__name__
    failures.append(MeteredFailure(region=region, exception_class=exception_class))
    log.error("CloudWatch %s unavailable in %s (%s): %s", operation, region, exception_class, exc)


def _metered_spend(day_start: dt.datetime, now: dt.datetime, rates: dict) -> MeteredSpend | None:
    total = float(0)
    metered_estimated = False
    found_series = False
    contributed = False
    failures: list[MeteredFailure] = []
    for region in REGIONS:
        cw = boto3.client("cloudwatch", region_name=region)
        queries, meta = [], {}
        paginator = cw.get_paginator("list_metrics")
        try:
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
                    found_series = True
                    qid = f"q{len(queries)}"
                    meta[qid] = (dims[0]["Value"], name)
                    queries.append(
                        {
                            "Id": qid,
                            "MetricStat": {
                                "Metric": {
                                    "Namespace": "AWS/Bedrock",
                                    "MetricName": name,
                                    "Dimensions": dims,
                                },
                                "Period": 3600,
                                "Stat": "Sum",
                            },
                            "ReturnData": True,
                        }
                    )
        except ClientError as exc:
            _record_metered_failure(failures, region=region, operation="metric listing", exc=exc)
            continue

        for batch_start in range(0, len(queries), 100):
            batch = queries[batch_start : batch_start + 100]
            try:
                resp = cw.get_metric_data(
                    MetricDataQueries=batch,
                    StartTime=day_start,
                    EndTime=now,
                    ScanBy="TimestampAscending",
                )
            except ClientError as exc:
                _record_metered_failure(failures, region=region, operation="metric data", exc=exc)
                continue
            for result in resp.get("MetricDataResults", []):
                units_1k = sum(result.get("Values", []) or []) / 1000
                if units_1k <= 0:
                    continue
                model_id, metric_name = meta[result["Id"]]
                metered_rate = _rate_for(model_id, metric_name, rates)
                total += units_1k * metered_rate.rate
                metered_estimated = metered_estimated or metered_rate.source in {
                    "seed",
                    "fallback",
                }
                contributed = True
    if failures and not contributed:
        return None
    if not found_series:
        log.info("CloudWatch Bedrock token metrics found no per-model series")
    return MeteredSpend(
        total=total,
        metered_estimated=metered_estimated,
        degraded=bool(failures),
        failures=tuple(failures),
    )


# --------------------------------------------------------------------------
# authoritative spend (slow signal)
# --------------------------------------------------------------------------
def _ce_spend_today(day: dt.date) -> float:
    request = {
        "TimePeriod": {"Start": day.isoformat(), "End": (day + dt.timedelta(days=1)).isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "Filter": {"Dimensions": {"Key": "SERVICE", "Values": BEDROCK_SERVICE_VALUES}},
    }
    spend = float(0)
    for resp in _ce_cost_and_usage_pages(request):
        for result in resp.get("ResultsByTime", []):
            # A present period carrying no Total means no billed Bedrock spend, not a
            # transport failure: report zero rather than raising past the ClientError
            # fallback the caller relies on.
            total = result.get("Total") or {}
            amount = total.get("UnblendedCost", {}).get("Amount")
            if amount is not None:
                spend += float(amount)
    return spend


# --------------------------------------------------------------------------
# enforcement
# --------------------------------------------------------------------------
class PolicyApplicationError(RuntimeError):
    """Raised after every target has been attempted and at least one failed."""


def _target_count() -> int:
    return len(TARGET_ROLES) + len(TARGET_GROUPS)


def _is_attached() -> bool:
    """Return true only when every configured target carries the deny."""
    if _target_count() == 0:
        return False
    for role in TARGET_ROLES:
        try:
            attached = iam.list_attached_role_policies(RoleName=role)
            if not any(p["PolicyArn"] == DENY_POLICY_ARN for p in attached["AttachedPolicies"]):
                return False
        except iam.exceptions.NoSuchEntityException:
            log.error("target role %s does not exist", role)
            return False
    for group in TARGET_GROUPS:
        try:
            attached = iam.list_attached_group_policies(GroupName=group)
            if not any(p["PolicyArn"] == DENY_POLICY_ARN for p in attached["AttachedPolicies"]):
                return False
        except iam.exceptions.NoSuchEntityException:
            log.error("target group %s does not exist", group)
            return False
    return True


def _has_any_attachment() -> bool:
    for role in TARGET_ROLES:
        try:
            attached = iam.list_attached_role_policies(RoleName=role)
            if any(p["PolicyArn"] == DENY_POLICY_ARN for p in attached["AttachedPolicies"]):
                return True
        except iam.exceptions.NoSuchEntityException:
            log.error("target role %s does not exist", role)
    for group in TARGET_GROUPS:
        try:
            attached = iam.list_attached_group_policies(GroupName=group)
            if any(p["PolicyArn"] == DENY_POLICY_ARN for p in attached["AttachedPolicies"]):
                return True
        except iam.exceptions.NoSuchEntityException:
            log.error("target group %s does not exist", group)
    return False


def _apply(attach: bool) -> list[str]:
    """Attach or detach the deny on every target, surfacing any failures."""
    touched = []
    failures = []
    if DRY_RUN:
        log.warning(
            "DRY_RUN set; would %s %d principals", "attach" if attach else "detach", _target_count()
        )
        return touched

    for role in TARGET_ROLES:
        try:
            if attach:
                iam.attach_role_policy(RoleName=role, PolicyArn=DENY_POLICY_ARN)
            else:
                iam.detach_role_policy(RoleName=role, PolicyArn=DENY_POLICY_ARN)
            touched.append(f"role/{role}")
        except ClientError as exc:
            log.error("failed to %s role %s: %s", "attach" if attach else "detach", role, exc)
            failures.append(f"role/{role}")
    for group in TARGET_GROUPS:
        try:
            if attach:
                iam.attach_group_policy(GroupName=group, PolicyArn=DENY_POLICY_ARN)
            else:
                iam.detach_group_policy(GroupName=group, PolicyArn=DENY_POLICY_ARN)
            touched.append(f"group/{group}")
        except ClientError as exc:
            log.error("failed to %s group %s: %s", "attach" if attach else "detach", group, exc)
            failures.append(f"group/{group}")
    if failures:
        action = "attach" if attach else "detach"
        raise PolicyApplicationError(f"failed to {action} deny policy for: {', '.join(failures)}")
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
def handler(event, context):
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

    # --- slow signal: CE, rate-limited to protect against per-call charges --
    ce_spend = None
    ce_status = "unavailable"
    if state.get("ce_day") == day.isoformat():
        ce_spend = float(state.get("ce_spend", float(0)))
        ce_status = "cached"
    last_poll = state.get("ce_last_poll")
    due = (
        state.get("ce_day") != day.isoformat()
        or not last_poll
        or (now - dt.datetime.fromisoformat(last_poll)).total_seconds() >= CE_MIN_INTERVAL_SECONDS
    )
    # Always take a fresh authoritative read when the meter says we are close;
    # that is exactly the moment a stale CE number is most expensive to trust.
    # An unavailable meter does not force a read: it cannot say we are close,
    # and polling on it would bypass CE_MIN_INTERVAL_SECONDS on every tick for
    # as long as the CloudWatch outage lasts.
    metered_total = None if metered is None else metered.total
    metered_estimated = False if metered is None else metered.metered_estimated
    near_threshold = metered_total is not None and metered_total * 5 >= THRESHOLD_USD * 4
    if due or near_threshold:
        try:
            ce_spend = _ce_spend_today(day)
            state["ce_spend"] = ce_spend
            state["ce_day"] = day.isoformat()
            state["ce_last_poll"] = now.isoformat()
            ce_status = "available"
        except ClientError as exc:
            if ce_spend is None:
                ce_status = "unavailable"
                log.error("CE poll failed; no cached authoritative signal available: %s", exc)
            else:
                log.error("CE poll failed; reusing cached authoritative signal: %s", exc)

    if metered is None:
        metered_status = "unavailable"
        if ce_spend is not None and ce_spend >= THRESHOLD_USD:
            spend = ce_spend
            effective_source = "ce"
        else:
            spend = THRESHOLD_USD
            effective_source = "metered_unavailable"
    else:
        metered_status = "degraded" if metered.degraded else "available"
        if ce_spend is not None and ce_spend > metered.total:
            spend = ce_spend
            effective_source = "ce"
        else:
            spend = metered.total
            effective_source = "metered"
    signal_unavailable = (
        metered_status == "unavailable"
        or (metered is not None and metered.degraded)
        or ce_status == "unavailable"
    )
    if metered_total is None:
        metered_text = "unavailable"
    else:
        metered_text = f"{metered_total:,.2f}"
        if metered_estimated:
            metered_text += " (estimated)"
    ce_text = "unavailable" if ce_spend is None else f"{ce_spend:,.2f}"
    attached = _is_attached()
    any_attached = attached or _has_any_attachment()
    log.info(
        "day=%s metered=%s ce=%s effective=%.4f threshold=%.2f attached=%s "
        "metered_status=%s ce_status=%s effective_source=%s",
        day,
        "unavailable" if metered_total is None else f"{metered_total:.4f}",
        "unavailable" if ce_spend is None else f"{ce_spend:.4f}",
        spend,
        THRESHOLD_USD,
        attached,
        metered_status,
        ce_status,
        effective_source,
    )

    # --- early warning (notify-only, once per UTC day) ----------------------
    if (
        WARN_THRESHOLD_USD
        and WARN_THRESHOLD_USD <= spend < THRESHOLD_USD
        and not signal_unavailable
        and state.get("warned_day") != day.isoformat()
    ):
        state["warned_day"] = day.isoformat()
        _notify(
            "Bedrock daily spend WARNING",
            f"Bedrock spend for {day} (UTC) reached {spend:,.2f} — "
            f"{spend / THRESHOLD_USD:.0%} of the {THRESHOLD_USD:,.2f} hard cap.\n"
            f"  metered (CloudWatch, ~5 min lag): {metered_text}\n"
            f"  billed  (Cost Explorer, 8-24h lag): {ce_text}\n\n"
            "No enforcement action has been taken. The hard cap is a circuit breaker "
            "and should never be reached in normal operation: investigate today's "
            "volume now, before the cap trips.",
        )

    # --- enforce -----------------------------------------------------------
    if any_attached and spend < THRESHOLD_USD and not signal_unavailable:
        touched = _apply(attach=False)
        if touched:
            _notify(
                "Bedrock daily cap released",
                f"New UTC day ({day}); Bedrock spend is {spend:,.2f}, under the "
                f"{THRESHOLD_USD:,.2f} cap. Deny policy detached from:\n  " + "\n  ".join(touched),
            )
        else:
            # _apply is a no-op under DRY_RUN, so the deny is still attached:
            # announcing a release here would contradict the deny_attached this
            # same invocation reports.
            log.warning("release skipped notification; nothing was detached")
    elif spend >= THRESHOLD_USD and not attached:
        first_trip = not any_attached
        touched = _apply(attach=True)
        if not DRY_RUN:
            state["tripped_day"] = day.isoformat()
        if not first_trip:
            log.warning("deny was only partially attached; re-applied to: %s", touched)
        if first_trip:
            _notify(
                f"Bedrock daily cap TRIPPED — {spend:,.2f} >= {THRESHOLD_USD:,.2f}",
                f"Bedrock spend for {day} (UTC) reached {spend:,.2f}.\n"
                f"  metered (CloudWatch, ~5 min lag): {metered_text}\n"
                f"  billed  (Cost Explorer, 8-24h lag): {ce_text}\n\n"
                f"Deny policy {DENY_POLICY_ARN} attached to:\n  "
                + "\n  ".join(touched or ["(nothing — see logs)"])
                + "\n\nThis lifts automatically at 00:00 UTC. To lift it sooner, detach "
                "the policy from the principals above.",
            )

    state["metered_estimated"] = metered_estimated
    _save_state(state)
    return {
        "day": day.isoformat(),
        "metered_usd": None if metered_total is None else round(metered_total, 4),
        "metered_estimated": metered_estimated,
        "metered_status": metered_status,
        "ce_usd": None if ce_spend is None else round(ce_spend, 4),
        "ce_status": ce_status,
        "effective_usd": round(spend, 4),
        "effective_source": effective_source,
        "threshold_usd": THRESHOLD_USD,
        "deny_attached": _is_attached(),
    }
