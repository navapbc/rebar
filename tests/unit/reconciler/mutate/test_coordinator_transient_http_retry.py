"""Coordinator-routed Jira writes retry HTTP 429/5xx within the pass (story inept-unanarchic-foal).

S7 retired the handler-level retry wrapper, so a 429 or 5xx on a coordinator-routed
update aborted the pass. The production coordinator dispatch now reports a 429 or 5xx as
an ``AtomicSignal(status="transient")`` (``failure_policy.classify_http_error``), so the
ADR 0103 retry budget retries it within the pass: at most 3 invocations, 15 s of
cumulative sleep, with ``Retry-After`` as a lower bound. A non-429 4xx stays a fail-fast
re-raise, and a budget that cannot retry re-raises the original ``HTTPError``, aborting
the pass as before.

These tests drive ``batch_dispatch.run_migrated_reroute`` with the production dispatch, a
fake transport, and the budget clock replaced by a recording clock (no real sleep).
"""

from __future__ import annotations

import importlib
import urllib.error
from email.message import Message

import pytest

pytestmark = pytest.mark.unit


class _RecordingClock:
    """The budget clock: ``now`` is frozen and ``sleep_ms`` is recorded, never slept."""

    def __init__(self) -> None:
        self.slept: list[int] = []

    def now(self) -> int:
        return 0

    def sleep_ms(self, ms: int) -> None:
        self.slept.append(ms)


@pytest.fixture
def batch_dispatch():
    return importlib.import_module("rebar.reconciler.batch_dispatch")


@pytest.fixture
def clock(batch_dispatch, monkeypatch: pytest.MonkeyPatch) -> _RecordingClock:
    recording = _RecordingClock()
    monkeypatch.setattr(batch_dispatch, "_retry_clock", lambda: recording)
    return recording


def _http_error(code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://jira.example/rest", code, "error", headers, None)


class _Transport:
    """``update_issue`` raises each scripted error in turn, then succeeds."""

    def __init__(self, errors: list[urllib.error.HTTPError], *, forever: bool = False) -> None:
        self.errors = list(errors)
        self.forever = forever
        self.calls: list[str] = []

    def update_issue(self, key, **_fields):
        self.calls.append(key)
        if self.forever:
            raise self.errors[0]
        if self.errors:
            raise self.errors.pop(0)
        return {"key": key}


def _update_plan(key: str):
    mutation_mod = importlib.import_module("rebar.reconciler.mutation")
    ticket_plan_mod = importlib.import_module("rebar.reconciler.ticket_plan")
    mutation = mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection.outbound,
        action=mutation_mod.MutationAction.update,
        target=key,
        payload={"changed_fields": {"summary": "s"}},
        provenance={"src": "outbound"},
    )
    return ticket_plan_mod.TicketPlan(
        identity=key,
        mutations=(mutation,),
        diagnostics=(),
        disposition=ticket_plan_mod.PlanDisposition("mutate"),
        observation_version="ov-1",
        payload={},
        dependencies=(),
        defer_reason=None,
    )


def _reroute(batch_dispatch, transport, tmp_path, key: str) -> tuple[dict, list[dict]]:
    apply_handlers = importlib.import_module("rebar.reconciler.apply_handlers")
    ctx = apply_handlers.BatchApplyContext(client=transport, repo_root=tmp_path, pass_id="p")
    sink: list[dict] = []
    tally = batch_dispatch.run_migrated_reroute(
        migrated_plans=[_update_plan(key)],
        ctx=ctx,
        abort_check=None,
        recheck_drift=lambda _c, _r, pin: pin,
        snapshot_head=None,
        repo_root=tmp_path,
        head_pin_cell=["pin0"],
        outcomes_sink=sink,
    )
    return tally, sink


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_a_transient_status_then_success_is_retried_within_the_pass(
    batch_dispatch, clock, tmp_path, code
) -> None:
    transport = _Transport([_http_error(code)])

    tally, sink = _reroute(batch_dispatch, transport, tmp_path, "T-1")

    assert transport.calls == ["T-1", "T-1"]
    assert len(sink) == 1 and not sink[0].get("error"), sink
    assert tally["applied_count"] == 1 and tally["failed_count"] == 0, tally
    assert len(clock.slept) == 1 and clock.slept[0] >= 2000, clock.slept


def test_retry_after_is_a_lower_bound_on_the_budget_sleep(batch_dispatch, clock, tmp_path) -> None:
    transport = _Transport([_http_error(429, retry_after="5")])

    _reroute(batch_dispatch, transport, tmp_path, "T-2")

    assert transport.calls == ["T-2", "T-2"]
    assert clock.slept and clock.slept[0] >= 5000, clock.slept


def test_a_non_429_4xx_stays_fail_fast(batch_dispatch, clock, tmp_path) -> None:
    err = _http_error(400)
    transport = _Transport([err], forever=True)

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _reroute(batch_dispatch, transport, tmp_path, "T-3")

    assert excinfo.value is err
    assert transport.calls == ["T-3"]
    assert clock.slept == []


def test_an_exhausted_budget_re_raises_the_original_error(batch_dispatch, clock, tmp_path) -> None:
    err = _http_error(503)
    transport = _Transport([err], forever=True)

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _reroute(batch_dispatch, transport, tmp_path, "T-4")

    assert excinfo.value is err
    assert transport.calls == ["T-4", "T-4", "T-4"]
    assert len(clock.slept) == 2


def test_a_retry_after_beyond_the_sleep_cap_re_raises_without_sleeping(
    batch_dispatch, clock, tmp_path
) -> None:
    err = _http_error(429, retry_after="30")
    transport = _Transport([err], forever=True)

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _reroute(batch_dispatch, transport, tmp_path, "T-5")

    assert excinfo.value is err
    assert transport.calls == ["T-5"]
    assert clock.slept == []
