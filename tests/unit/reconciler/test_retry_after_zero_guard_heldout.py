"""HELD-OUT pin on the reconciler's ``Retry-After: 0`` zero-guard (bug 3604-891f-e301-4aff).

A ``Retry-After: 0`` is a VALID directive meaning "retry immediately", but the reconciler's
ADR-0036 backoff exists to *desynchronize* concurrent reconcilers hitting one Jira instance. If a
zero directive is honored literally it collapses the backoff to ``time.sleep(0.0)`` — a
synchronized immediate replay, the same thundering-herd class-B vector ticket 254e guarded for the
Anthropic transport (``a zero/negative Retry-After is treated as absent``). So a ``0`` must be
treated like an ABSENT header — routed to the already-jittered fallback — and MUST be
distinguished from a present positive value, which is still honored (and capped).

This test is held out from the fix: it exercises the 429 backoff surfaces plus the shared
parser, and it is designed to be RED before the guard exists (each surface produces a ``0.0``
immediate replay) and GREEN after. ADR 0103 S3 retired the handler-level retry wrapper that was
surface 2, so the DC and ACLI surfaces remain.
"""

from __future__ import annotations

import pytest

# The reconciler engine is on sys.path via the package conftest; import flat.
from rebar.reconciler._errors import MAX_BACKOFF_S, parse_retry_after
from rebar.reconciler.adapters.jira import acli_subprocess
from rebar.reconciler.adapters.jira_datacenter import retry as _dc_retry


# ── the shared parser (single source feeding the DC surface) ─────────────────────────────────
def test_parse_retry_after_maps_zero_and_negative_to_absent() -> None:
    """A non-positive ``Retry-After`` parses to ``None`` — the same result as an absent header —
    so every caller routes it to the jittered fallback instead of a zero-second sleep."""
    assert parse_retry_after("0") is None
    assert parse_retry_after("0.0") is None
    assert parse_retry_after(" 0 ") is None
    assert parse_retry_after("-5") is None
    # Absent / unparseable already mapped to None; pin that zero now joins them.
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None


def test_parse_retry_after_still_honors_a_positive_value() -> None:
    """The regression guard: a present positive value is unchanged (honored, not dropped)."""
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after("0.5") == 0.5
    assert parse_retry_after("30") == 30.0


# ── surface 3: jira_datacenter/retry.py._with_connection_retry ───────────────────────────────
class _FakeResponse:
    def __init__(self, headers: dict[str, str] | None) -> None:
        self.headers = headers if headers is not None else {}


class _Fake429(Exception):
    """Stands in for ``jira.exceptions.JIRAError`` with ``Retry-After`` on the RESPONSE headers."""

    def __init__(self, status_code: int = 429, retry_after: str | None = "0") -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.url = "https://dc.example.invalid/rest/api/2/search"
        self.response = _FakeResponse({"Retry-After": retry_after} if retry_after else {})
        self.headers = {"Retry-After": "9999"}  # deliberately WRONG place


def test_dc_429_with_retry_after_zero_does_not_immediately_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An opted-in DC 429 carrying ``Retry-After: 0`` must not sleep ``0.0`` and hammer back.

    Post-guard, ``_retry_after_seconds`` returns ``None`` for a ``0`` header — indistinguishable
    from "no usable header" — so the ``is not None`` retry predicate does not fire and the error
    is translated on the first occurrence (DC has no jittered 429 fallback: no header => raise,
    never a zero-delay replay). RED before the guard: the ``0.0`` slips through and it sleeps 0.
    """
    from rebar.reconciler._backend import BackendHTTPError

    slept: list[float] = []
    monkeypatch.setattr(_dc_retry.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(_dc_retry, "_jira_http_error_types", lambda: (_Fake429,))

    calls: list[int] = []

    def _boom() -> None:
        calls.append(1)
        raise _Fake429(retry_after="0")

    with pytest.raises(BackendHTTPError):
        _dc_retry._with_connection_retry(_boom, rate_limit_retry=True)

    assert slept == [], f"DC slept {slept} on a Retry-After: 0 — an immediate synchronized replay"
    assert len(calls) == 1, f"DC replayed a Retry-After: 0 ({len(calls)} attempts)"


def test_dc_429_with_positive_retry_after_still_retries_jittered_and_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: an opted-in DC 429 with a positive header still retries, jittered and capped."""
    slept: list[float] = []
    monkeypatch.setattr(_dc_retry.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(_dc_retry, "_jira_http_error_types", lambda: (_Fake429,))

    calls: list[int] = []

    def _twice_then_ok() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise _Fake429(retry_after="2")
        return "ok"

    assert _dc_retry._with_connection_retry(_twice_then_ok, rate_limit_retry=True) == "ok"
    assert len(slept) == 2, f"expected two backoffs, got {slept}"
    for delay in slept:
        assert 2.0 <= delay <= 2.0 * 1.2, f"delay {delay} is not Retry-After(2s) + up to 20% jitter"
        assert delay <= MAX_BACKOFF_S


# ── surface 4: acli_subprocess._rate_limit_backoff (its own inline regex, NOT parse_retry_after)
def test_acli_rate_limit_backoff_with_retry_after_zero_uses_jittered_fallback() -> None:
    """The acli subprocess path parses ``Retry-After`` with its own regex, so it needs its own
    zero-guard: a matched ``0`` must fall through to the jittered exponential backoff, never
    ``min(0.0, MAX) = 0.0``."""
    delay = acli_subprocess._rate_limit_backoff(0, "HTTP 429 too many requests\nRetry-After: 0")
    assert delay is not None
    assert delay > 0.0, f"acli honored Retry-After: 0 as {delay} — an immediate synchronized replay"


def test_acli_rate_limit_backoff_still_honors_positive_and_caps() -> None:
    """Regression: a positive ``Retry-After`` is still honored verbatim and clamped to MAX."""
    assert acli_subprocess._rate_limit_backoff(0, "HTTP 429\nRetry-After: 7") == 7.0
    assert acli_subprocess._rate_limit_backoff(0, "429 Retry-After: 99999") == 60.0


def test_acli_rate_limit_backoff_no_header_is_unchanged_jittered() -> None:
    """The no-header jittered fallback is unchanged: 429 with no parseable header backs off > 0."""
    delay = acli_subprocess._rate_limit_backoff(0, "HTTP 429 too many requests")
    assert delay is not None and delay > 0.0
