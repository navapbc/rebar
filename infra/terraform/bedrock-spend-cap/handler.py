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

HOW TOKENS BECOME DOLLARS
-------------------------
Every token is priced at a CEILING rate for its direction, set ABOVE the highest
per-token rate this account has ever been billed for that direction. So the
metered figure is an UPPER BOUND on real spend, not an estimate of it, and the
cap cannot fire late on the metered arm.

There is deliberately no derived rate table and no per-model matching. The
previous design derived rates from Cost Explorer and matched them to CloudWatch
ModelIds by substring and suffix heuristics; that produced four distinct
mispricings in twelve days, two of which reached production, because AWS bills
the same model family under several label conventions. A single ceiling per
direction removes the entire class: there is nothing left to match.

The cost is precision in the safe direction. A model much cheaper than the
priciest is over-priced by the ratio between them, so a large embedding or
small-model batch job can trip the cap well below the configured dollar figure.
That is accepted: erring early is correct for a circuit breaker.

THRESHOLD_USD IS NOT SCALED TO COMPENSATE
-----------------------------------------
Raising the threshold to centre the metered arm would raise it for `ce_spend`
too, and `ce_spend` is real billed dollars. The authoritative arm would then
trip LATE, which is the failure this design exists to remove. Unscaled, the CE
arm trips at exactly the configured cap and the metered arm trips early.

WHY CE IS NOT POLLED EVERY TICK
-------------------------------
Cost Explorer charges per API request. Polling it on the 5-minute tick is an
inefficient way to run a cost control. CE is therefore polled at most once per
CE_MIN_INTERVAL_SECONDS (default hourly). The 5-minute tick runs on CloudWatch
alone, which is the signal that actually needs to be fast.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass

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
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Bedrock spend lands under the legacy `Amazon Bedrock` service and under one
# marketplace service per model, named "<Model> (Amazon Bedrock Edition)". The set
# grows whenever AWS lists a new model, so it is matched by PREDICATE rather than
# enumerated: an enumerated allowlist silently omitted five live services and left
# the authoritative signal reading about a third of real spend.
BEDROCK_EDITION_SUFFIX = " (Amazon Bedrock Edition)"


def is_bedrock_service(service: str) -> bool:
    """True for every Cost Explorer SERVICE that carries Bedrock spend."""
    return service == "Amazon Bedrock" or service.endswith(BEDROCK_EDITION_SUFFIX)


# USD per token, per token direction. Each value is 1.25x the highest per-token
# rate this account was billed for that direction over the trailing 60 days of
# Cost Explorer, derived 2026-09-25; Claude Opus binds every direction, being the
# priciest family in use. The 1.25 factor is deliberate headroom: at parity a
# price rise would break the upper-bound property silently, on the tick it landed.
#
# Re-derive with: for each direction, max(UnblendedCost / UsageQuantity) across all
# services where is_bedrock_service() holds, remembering that marketplace services
# meter in units of 1,000,000 tokens and the legacy service in units of 1,000.
CEILING = {
    "InputTokenCount": 6.875e-06,
    "OutputTokenCount": 3.4375e-05,
    "CacheReadInputTokenCount": 6.875e-07,
    "CacheWriteInputTokenCount": 8.59375e-06,
}

# The day's high-water metered token total, persisted under this state key so an
# all-empty CloudWatch read can be told apart from a genuinely idle day.
HIGH_WATER_KEY = "tokens_high_water"

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
# metered spend (fast signal)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MeteredFailure:
    region: str
    exception_class: str


@dataclass(frozen=True)
class MeteredSpend:
    total: float
    degraded: bool
    failures: tuple[MeteredFailure, ...]
    # False when every query returned no datapoints. Zero tokens and "the metric
    # pipeline told us nothing" are the same number but not the same fact, and the
    # release decision turns on which one it is.
    saw_datapoints: bool


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


def _metered_spend(day_start: dt.datetime, now: dt.datetime) -> MeteredSpend | None:
    """Price today's Bedrock tokens at the ceiling rate for each direction.

    Reads the four UNDIMENSIONED AWS/Bedrock token series per region — the
    zero-dimension aggregate, requested with an explicit empty Dimensions list,
    which is not the same request as omitting the key. That aggregate already
    covers every model, so per-ModelId series are never added to this total; doing
    both would double-count. It also needs no list_metrics call, so a model that
    has stopped publishing recently cannot silently drop out of the sum.

    Returns None only when every region failed, which the caller treats as
    fail-closed. A partial failure returns degraded=True and a total that is a
    FLOOR, not the account total.
    """
    total = float(0)
    saw_datapoints = False
    contributed = False
    failures: list[MeteredFailure] = []
    for region in REGIONS:
        cw = boto3.client("cloudwatch", region_name=region)
        queries = [
            {
                "Id": f"m{index}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Bedrock",
                        "MetricName": metric,
                        "Dimensions": [],
                    },
                    "Period": 3600,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            }
            for index, metric in enumerate(CEILING)
        ]
        metric_by_id = {f"m{index}": metric for index, metric in enumerate(CEILING)}
        try:
            resp = cw.get_metric_data(
                MetricDataQueries=queries,
                StartTime=day_start,
                EndTime=now,
                ScanBy="TimestampAscending",
            )
        except ClientError as exc:
            _record_metered_failure(failures, region=region, operation="metric data", exc=exc)
            continue
        contributed = True
        for result in resp.get("MetricDataResults", []):
            values = result.get("Values") or []
            if values:
                saw_datapoints = True
            total += sum(values) * CEILING[metric_by_id[result["Id"]]]
    if failures and not contributed:
        return None
    return MeteredSpend(
        total=total,
        degraded=bool(failures),
        failures=tuple(failures),
        saw_datapoints=saw_datapoints,
    )


# --------------------------------------------------------------------------
# authoritative spend (slow signal)
# --------------------------------------------------------------------------
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


def _ce_spend_today(day: dt.date) -> float:
    # No SERVICE filter. A filter needs an enumerated value list, and that list is
    # exactly what went stale: it omitted five live marketplace services, so this
    # signal read about a third of real spend. Ask for every service grouped by
    # SERVICE instead and classify the returned groups by predicate, which cannot
    # fall behind AWS listing a new model. Same one request, same cost.
    request = {
        "TimePeriod": {"Start": day.isoformat(), "End": (day + dt.timedelta(days=1)).isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    spend = float(0)
    for resp in _ce_cost_and_usage_pages(request):
        for result in resp.get("ResultsByTime", []):
            # A period carrying no matching group means no billed Bedrock spend, not a
            # transport failure: report zero rather than raising past the ClientError
            # fallback the caller relies on.
            for group in result.get("Groups") or []:
                keys = group.get("Keys") or []
                if not keys or not is_bedrock_service(keys[0]):
                    continue
                amount = group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount")
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

    # State written by the previous handler carries a derived rate table under
    # "rates"/"rates_day" plus a "metered_estimated" flag. Nothing reads them now, but
    # _save_state rewrites whatever _load_state returned, so they would persist
    # forever unless dropped explicitly. Drop them: the rate table was the bulk of a
    # state document already close to SSM's 4,096-byte Standard-tier ceiling, and a
    # put_parameter that outgrows it would wedge state persistence outright.
    for retired in ("rates", "rates_day", "metered_estimated"):
        state.pop(retired, None)

    # --- fast signal: every tick, CloudWatch only --------------------------
    metered = _metered_spend(day_start, now)

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

    # An all-empty CloudWatch read is ambiguous: it is BOTH a metric-delivery gap and
    # the normal idle / fresh-UTC-day state the automatic release depends on. Cost
    # Explorer cannot break the tie, because it lags 8-24h and reads zero for hours
    # into a busy day. So record the day's high-water token total, keyed by UTC date
    # so a new day starts absent, and let it discriminate:
    #
    #   key present, still zero  -> genuinely idle today   -> release permitted
    #   key present and nonzero  -> we saw traffic earlier  -> empty read is a gap
    #   key absent               -> nothing known yet; hold if a deny is attached,
    #                               which is also the mid-day-deploy case over state
    #                               written by the previous handler
    #
    # This governs RELEASE only. On the trip side an empty read simply prices to zero
    # and ce_spend remains the other arm of the max(), so a gap cannot manufacture a
    # trip.
    high_water_day = state.get("high_water_day")
    prior_high_water = (
        float(state.get(HIGH_WATER_KEY, float(0))) if high_water_day == day.isoformat() else None
    )
    observed_tokens = metered is not None and metered.saw_datapoints
    if metered is not None and (observed_tokens or prior_high_water is None):
        state["high_water_day"] = day.isoformat()
        state[HIGH_WATER_KEY] = max(prior_high_water or float(0), metered.total)
    empty_read = metered is not None and not metered.saw_datapoints
    release_blocked_by_gap = empty_read and (prior_high_water is None or prior_high_water > 0)
    if metered_total is None:
        metered_text = "unavailable"
    else:
        metered_text = f"{metered_total:,.2f} (ceiling-priced upper bound)"
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
    if (
        any_attached
        and spend < THRESHOLD_USD
        and not signal_unavailable
        and not release_blocked_by_gap
    ):
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

    _save_state(state)
    return {
        "day": day.isoformat(),
        "metered_usd": None if metered_total is None else round(metered_total, 4),
        "metered_status": metered_status,
        "ce_usd": None if ce_spend is None else round(ce_spend, 4),
        "ce_status": ce_status,
        "effective_usd": round(spend, 4),
        "effective_source": effective_source,
        "threshold_usd": THRESHOLD_USD,
        "deny_attached": _is_attached(),
    }
