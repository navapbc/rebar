"""The sole retry-budget owner for the reconciler mutate path plus the
observe-before-replay decision table.

The budget is bounded twice: at most :data:`MAX_INVOCATIONS` physical invocations
(the initial call plus its retries) and at most :data:`MAX_CUMULATIVE_SLEEP_MS`
total sleep across those retries. Delay is an additive-jitter floor grounded in
ADR 0084 / ``acli_subprocess._rate_limit_backoff``; a provider-supplied delay is
an authoritative *lower* bound that is never shortened.

Import convention: this package ships as package DATA under ``src/rebar/_engine``,
so the sibling value module is imported as ``rebar_reconciler.operation_outcome``
(not a relative import and not ``rebar._engine...``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from rebar_reconciler.operation_outcome import DelaySource, Disposition, ReplaySafety

MAX_INVOCATIONS = 3
MAX_CUMULATIVE_SLEEP_MS = 15000


class _Clock(Protocol):
    def now(self) -> int: ...

    def sleep_ms(self, ms: int) -> None: ...


@dataclass(frozen=True)
class RetryPlan:
    action: str
    delay_ms: int | None
    delay_source: DelaySource
    retry_not_before: str | None
    invocation_count: int
    request_count: int
    cumulative_sleep_ms: int


def _rfc3339_utc(epoch_ms: int) -> str:
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _floor_delay(retry_index: int, jitter: float) -> int:
    return int((2 ** (retry_index + 1) + jitter) * 1000)


def plan_next_retry(
    *,
    retry_index: int,
    invocation_count: int,
    request_count: int,
    cumulative_sleep_ms: int,
    jitter: float,
    now_ms: int,
    provider_delay_ms: int | None = None,
) -> RetryPlan:
    if invocation_count >= MAX_INVOCATIONS:
        return RetryPlan(
            action="exhausted",
            delay_ms=None,
            delay_source=DelaySource.none,
            retry_not_before=None,
            invocation_count=invocation_count,
            request_count=request_count,
            cumulative_sleep_ms=cumulative_sleep_ms,
        )

    base_ms = _floor_delay(retry_index, jitter)
    delay_source = DelaySource.fallback_jitter
    delay_ms = base_ms
    if provider_delay_ms is not None:
        delay_ms = max(base_ms, provider_delay_ms)
        if provider_delay_ms > base_ms:
            delay_source = DelaySource.provider

    if cumulative_sleep_ms + delay_ms > MAX_CUMULATIVE_SLEEP_MS:
        return RetryPlan(
            action="defer",
            delay_ms=delay_ms,
            delay_source=delay_source,
            retry_not_before=_rfc3339_utc(now_ms + delay_ms),
            invocation_count=invocation_count,
            request_count=request_count,
            cumulative_sleep_ms=cumulative_sleep_ms,
        )

    return RetryPlan(
        action="retry",
        delay_ms=delay_ms,
        delay_source=delay_source,
        retry_not_before=None,
        invocation_count=invocation_count + 1,
        request_count=request_count + 1,
        cumulative_sleep_ms=cumulative_sleep_ms + delay_ms,
    )


class RetryBudget:
    def __init__(self, *, clock: _Clock, jitter) -> None:
        self.clock = clock
        self.jitter = jitter
        self.invocation_count = 1
        self.request_count = 1
        self.cumulative_sleep_ms = 0

    def attempt_retry(self, *, provider_delay_ms: int | None = None) -> RetryPlan:
        retry_index = self.invocation_count - 1
        plan = plan_next_retry(
            retry_index=retry_index,
            invocation_count=self.invocation_count,
            request_count=self.request_count,
            cumulative_sleep_ms=self.cumulative_sleep_ms,
            jitter=self.jitter(),
            now_ms=self.clock.now(),
            provider_delay_ms=provider_delay_ms,
        )
        if plan.action == "retry":
            assert plan.delay_ms is not None
            self.clock.sleep_ms(plan.delay_ms)
            self.invocation_count = plan.invocation_count
            self.request_count = plan.request_count
            self.cumulative_sleep_ms = plan.cumulative_sleep_ms
        return plan


def decide_replay(*, observation: str, budget_remaining: bool) -> tuple[Disposition, bool]:
    if observation == "desired":
        return (Disposition.recovered, False)
    if observation == "old_conclusive":
        if budget_remaining:
            return (Disposition.retryable_deferred, True)
        return (Disposition.exhausted_transient, False)
    if observation in {"failed", "inconclusive"}:
        return (Disposition.commit_unknown, False)
    raise ValueError(f"unrecognized observation: {observation!r}")


def replay_safety_for(disposition: Disposition) -> ReplaySafety:
    if disposition == Disposition.commit_unknown:
        return ReplaySafety.forbidden
    return ReplaySafety.not_applicable
