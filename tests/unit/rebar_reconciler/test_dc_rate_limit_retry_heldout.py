"""HELD-OUT pin on the Data Center 429 / ``Retry-After`` policy (story S2, epic e369).

Jira Data Center has a built-in per-user token bucket. Before this story a 429 propagated as a
hard failure on the first occurrence, on an instance that was merely asking rebar to slow down.

THE VERSION/DEFAULT NUMBERS ARE LABELLED, NOT ASSERTED. "8.6+", "DC only" and "off by default"
carry their provenance in `retry.py`'s comment on `_RETRY_AFTER_JITTER`: one is confirmed live
against the harness, the rest are explicitly marked UNVERIFIED. None of them is load-bearing for
this code, because the retry keys off the PRESENCE of a `Retry-After` header rather than off any
assumption about the limiter's configuration.

THE CONSTRAINT THAT SHAPES EVERYTHING HERE: ``_with_connection_retry`` is the single choke point
for ALL transport call sites INCLUDING ``create_issue``, ``add_comment`` and ``add_label``. A 429
can arrive AFTER the server began a write, and nothing in the response distinguishes that from
rejection at the gate — so a blanket 429 retry would reintroduce the duplicate-issue class bug
[rebar:21fc-51d7-90ca-4a03] just fixed. The retry is therefore a per-call OPT-IN that DEFAULTS TO
OFF, and the tests below spend most of their effort on the default rather than on the happy path.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler._backend import BackendHTTPError
from rebar_reconciler._errors import MAX_BACKOFF_S
from rebar_reconciler.adapters.jira_datacenter import retry as _retry
from rebar_reconciler.adapters.jira_datacenter import transport as _t


class _FakeResponse:
    """Carries RESPONSE headers, which is where ``Retry-After`` actually lives."""

    def __init__(self, headers: dict[str, str] | None) -> None:
        self.headers = headers if headers is not None else {}


class _Fake429(Exception):
    """Stands in for ``jira.exceptions.JIRAError`` without needing the extra installed.

    Mirrors the real error's SHAPE precisely where it matters: ``status_code`` plus a
    ``response`` whose ``.headers`` are the RESPONSE headers. The real class also has a
    ``.headers`` attribute, but pycontribs sets it from its own ``**kwargs`` and documents it as
    the REQUEST headers — so a stand-in that put ``Retry-After`` there would have let a wrong
    implementation pass.
    """

    def __init__(self, status_code: int = 429, retry_after: str | None = "2") -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.url = "https://dc.example.invalid/rest/api/2/search"
        self.response = _FakeResponse({"Retry-After": retry_after} if retry_after else {})
        # Deliberately WRONG place for the header, to catch an implementation that reads it:
        self.headers = {"Retry-After": "9999"}


@pytest.fixture
def as_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_Fake429`` the transport's recognised HTTP-error type.

    The unit tier has no ``[jira-datacenter]`` extra, so ``_jira_http_error_types()`` returns an
    empty tuple and ``except ()`` matches nothing. Patching it is what lets these tests exercise
    the real branch instead of a path that never runs.
    """
    monkeypatch.setattr(_retry, "_jira_http_error_types", lambda: (_Fake429,))


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture the delays instead of serving them, so the test asserts the ARITHMETIC."""
    slept: list[float] = []
    monkeypatch.setattr(_retry.time, "sleep", lambda s: slept.append(s))
    return slept


# ---------------------------------------------------------------------------
# The default is OFF — the assertions that keep mutations safe
# ---------------------------------------------------------------------------


def test_a_429_is_not_retried_by_default(as_http_errors: None, no_sleep: list[float]) -> None:
    """No flag -> ONE attempt, then translate and raise. This is the mutation-safety guarantee.

    Asserted on the CALL COUNT, not merely on "it raised": an implementation that retried and
    then re-raised would satisfy a raises-only assertion while having issued the write twice.
    """
    calls: list[int] = []

    def _boom() -> None:
        calls.append(1)
        raise _Fake429()

    with pytest.raises(BackendHTTPError) as caught:
        _retry._with_connection_retry(_boom)

    assert len(calls) == 1, f"a 429 was retried without opting in ({len(calls)} attempts)"
    assert caught.value.code == 429
    assert no_sleep == [], "slept before failing a non-opted-in 429"


def test_every_mutating_transport_member_still_fails_on_the_first_429(
    as_http_errors: None, no_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENUMERATED, not sampled. An ellipsis is how a later-added mutation inherits the retry.

    Drives each member through the real transport object so the assertion covers the wiring as
    shipped, rather than re-asserting the helper in isolation.
    """
    attempts: dict[str, int] = {}

    class _RateLimitedClient:
        def __getattr__(self, name: str) -> Any:
            def _call(*_a: Any, **_k: Any) -> Any:
                attempts[name] = attempts.get(name, 0) + 1
                raise _Fake429()

            return _call

    transport = _t.JiraDataCenterTransport(client=_RateLimitedClient(), project="DIG")

    mutations = {
        "create_issue": lambda: transport.create_issue({"summary": "x", "issuetype": "Task"}),
        "update_issue": lambda: transport.update_issue("DIG-1", summary="x"),
        "add_comment": lambda: transport.add_comment("DIG-1", "body"),
        "add_label": lambda: transport.add_label("DIG-1", "lbl"),
        "remove_label": lambda: transport.remove_label("DIG-1", "lbl"),
        "transition_issue_by_name": lambda: transport.transition_issue_by_name("DIG-1", "Done"),
        "set_relationship": lambda: transport.set_relationship("DIG-1", "DIG-2"),
        "delete_issue": lambda: transport.delete_issue("DIG-1"),
        "delete_issue_link": lambda: transport.delete_issue_link("10000"),
        "set_parent": lambda: transport.set_parent("DIG-1", "DIG-2"),
    }

    for name, call in mutations.items():
        attempts.clear()
        no_sleep.clear()
        with pytest.raises(BackendHTTPError) as caught:
            call()
        assert caught.value.code == 429, (
            f"{name} surfaced HTTP {caught.value.code}, not the 429 the client raised — the "
            f"translation lost the status, so callers cannot tell a rate limit from anything "
            f"else"
        )
        total = sum(attempts.values())
        assert total == 1, (
            f"{name} issued {total} client calls under a 429 — exactly one is the contract. "
            f"More than one is the duplicate-issue class bug 21fc fixed; ZERO means the "
            f"member failed BEFORE reaching the client, so the 429 branch was never "
            f"exercised and this member is not actually covered"
        )
        assert no_sleep == [], f"{name} slept on a 429; mutations must fail fast"


# ---------------------------------------------------------------------------
# Opted in: the header is honoured, jittered and clamped
# ---------------------------------------------------------------------------


def test_an_opted_in_read_retries_a_429_using_the_response_header(
    as_http_errors: None, no_sleep: list[float]
) -> None:
    """The retry fires, and the delay comes from ``Retry-After`` on the RESPONSE.

    The fake puts a decoy ``Retry-After: 9999`` on ``exc.headers`` — where pycontribs keeps the
    REQUEST headers — so an implementation reading the wrong attribute produces a ~9999s delay
    and fails this assertion loudly instead of passing by coincidence.
    """
    calls: list[int] = []

    def _twice_then_ok() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise _Fake429(retry_after="2")
        return "ok"

    assert _retry._with_connection_retry(_twice_then_ok, rate_limit_retry=True) == "ok"
    assert len(calls) == 3
    assert len(no_sleep) == 2, f"expected two backoffs, got {no_sleep}"
    for delay in no_sleep:
        assert 2.0 <= delay <= 2.0 * 1.2, (
            f"delay {delay} is not Retry-After(2s) plus up to 20% jitter — a decoy header of "
            f"9999 sits on exc.headers, so a wildly larger value means the REQUEST headers "
            f"were read instead of the response's"
        )


def test_the_delay_is_clamped_to_max_backoff(as_http_errors: None, no_sleep: list[float]) -> None:
    """A server asking for an hour must not park the pass for an hour.

    Jitter is applied BEFORE the clamp, so the ceiling is a real ceiling: clamping first and
    jittering after would exceed MAX_BACKOFF_S by up to 20%.
    """

    def _boom() -> None:
        raise _Fake429(retry_after="3600")

    with pytest.raises(BackendHTTPError):
        _retry._with_connection_retry(_boom, rate_limit_retry=True)

    assert no_sleep, "an opted-in 429 with a header did not retry at all"
    assert max(no_sleep) <= MAX_BACKOFF_S, (
        f"delay {max(no_sleep)} exceeds MAX_BACKOFF_S={MAX_BACKOFF_S}"
    )


def test_no_retry_after_header_degrades_to_todays_behaviour(
    as_http_errors: None, no_sleep: list[float]
) -> None:
    """Limiter off / pre-8.6 / Server: no header -> fail on the first 429, exactly as before.

    Deliberate: a static pacing policy is not implementable against an admin-toggled limiter, so
    with no server guidance the honest behaviour is the old one rather than an invented curve.
    """
    calls: list[int] = []

    def _boom() -> None:
        calls.append(1)
        raise _Fake429(retry_after=None)

    with pytest.raises(BackendHTTPError) as caught:
        _retry._with_connection_retry(_boom, rate_limit_retry=True)

    assert len(calls) == 1, "retried a 429 that carried no Retry-After header"
    assert caught.value.code == 429
    assert no_sleep == []


def test_cap_exhaustion_raises_the_neutral_error_and_never_the_vendor_type(
    as_http_errors: None, no_sleep: list[float]
) -> None:
    """After the cap: ``BackendHTTPError`` with ``code == 429`` — never the library error.

    Asserting the vendor type does NOT escape is the point. J10 pinned the neutral exception
    contract precisely so the core never learns one ``except`` clause per vendor, and a test
    that only checks "it raised" passes while ``JIRAError`` leaks past the adapter boundary.
    """

    def _always() -> None:
        raise _Fake429(retry_after="1")

    with pytest.raises(BackendHTTPError) as caught:
        _retry._with_connection_retry(_always, rate_limit_retry=True)

    assert caught.value.code == 429
    assert not isinstance(caught.value, _Fake429), "the vendor error escaped the adapter boundary"


def test_a_non_429_http_error_is_never_retried_even_when_opted_in(
    as_http_errors: None, no_sleep: list[float]
) -> None:
    """The opt-in grants 429 handling ONLY. A 500 must still fail on the first attempt."""
    calls: list[int] = []

    def _boom() -> None:
        calls.append(1)
        raise _Fake429(status_code=500, retry_after="2")

    with pytest.raises(BackendHTTPError) as caught:
        _retry._with_connection_retry(_boom, rate_limit_retry=True)

    assert len(calls) == 1, "a 500 was retried by the rate-limit branch"
    assert caught.value.code == 500


# ---------------------------------------------------------------------------
# BOTH paths carry the flag — they fail independently, so they are tested independently
# ---------------------------------------------------------------------------


def test_call_logged_forwards_the_flag(as_http_errors: None, no_sleep: list[float]) -> None:
    """PATH A's missing link. Before S2 ``_call_logged`` called ``_with_connection_retry(fn)``
    with no keyword, so a flag threaded only to here was a no-op that still type-checked."""
    calls: list[int] = []

    def _once_then_ok() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise _Fake429(retry_after="1")
        return "ok"

    assert _t._call_logged("probe", "DIG-1", _once_then_ok, rate_limit_retry=True) == "ok"
    assert len(calls) == 2, "the flag did not reach _with_connection_retry through _call_logged"

    # …and its default is still OFF, so every OTHER transport member is unaffected.
    calls.clear()
    no_sleep.clear()
    with pytest.raises(BackendHTTPError):
        _t._call_logged("probe", "DIG-1", _once_then_ok)
    assert len(calls) == 1, "_call_logged retried a 429 without being asked to"


def test_paged_search_forwards_the_flag_and_defaults_to_off(
    as_http_errors: None, no_sleep: list[float]
) -> None:
    """PATH A end to end, through the real ``_paged_search``."""
    attempts: list[int] = []

    class _Client:
        def search_issues(self, *_a: Any, **_k: Any) -> list[Any]:
            attempts.append(1)
            if len(attempts) < 2:
                raise _Fake429(retry_after="1")
            return []

    transport = _t.JiraDataCenterTransport(client=_Client(), project="DIG")

    assert transport._paged_search("project = DIG", rate_limit_retry=True) == []
    assert len(attempts) == 2, "_paged_search did not forward the opt-in"

    attempts.clear()
    with pytest.raises(BackendHTTPError):
        transport._paged_search("project = DIG")
    assert len(attempts) == 1, "_paged_search retried without the opt-in"


def test_the_per_issue_comment_fetch_opts_in_directly(
    as_http_errors: None, no_sleep: list[float]
) -> None:
    """PATH B: ``get_comment_map``'s inner per-issue fetch bypasses ``_call_logged`` entirely.

    THE CELL THAT JUSTIFIES THE SECOND PATH. It is one request PER ISSUE — the highest-volume
    read in a pass, and so the call most likely to trip a token bucket — and it calls
    ``_with_connection_retry`` directly. An implementation that threaded the flag only through
    ``_call_logged`` leaves exactly this one unprotected while every ``_paged_search`` test above
    still passes.
    """
    comment_attempts: list[int] = []

    class _Client:
        def search_issues(self, *_a: Any, **_k: Any) -> list[Any]:
            return [{"key": "DIG-1"}] if not _k.get("startAt") else []

        def comments(self, _key: str) -> list[Any]:
            comment_attempts.append(1)
            if len(comment_attempts) < 2:
                raise _Fake429(retry_after="1")
            return []

    transport = _t.JiraDataCenterTransport(client=_Client(), project="DIG")
    out = transport.get_comment_map("DIG")

    assert len(comment_attempts) == 2, (
        "the per-issue comment fetch did not retry an opted-in 429 — the highest-volume read in "
        "a pass is the one PATH A cannot reach"
    )
    assert "DIG-1" in out
