"""Story 9622: _call_with_retry gains urllib.error.HTTPError retry.

The REST-transport floor (acli_rest) raises RAW urllib.error.HTTPError, which the
wrapper previously did not catch — so the idempotent REST writes routed through it
(set_entity_property, set_parent, update_issue's REST legs) got zero retry.

Covers:
- 429 with a present integer Retry-After header -> honored
- 429 without a Retry-After header -> jittered ADR-0036 backoff
- 5xx -> jittered backoff, then exhaustion re-raises the RAW HTTPError (not
  RetryExhaustedError, so downstream raw-HTTPError catchers still work)
- 404 (and other non-429 4xx) -> re-raised raw immediately, no retry
- acli_rest's raw-HTTPError re-raise is UNCHANGED (no boundary translation)
"""

from __future__ import annotations

import email.message
import importlib.util
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DISPATCH_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "dispatch_one.py"


def _load_dispatch():
    spec = importlib.util.spec_from_file_location("dispatch_one_httperror_test", DISPATCH_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dispatch_one_httperror_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def dispatch():
    if not DISPATCH_PATH.exists():
        pytest.fail(f"dispatch_one.py not found at {DISPATCH_PATH}")
    return _load_dispatch()


def _http_error(code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = None
    if retry_after is not None:
        hdrs = email.message.Message()
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://example.atlassian.net/rest/api/3/issue/DIG-1/properties/local_id",
        code=code,
        msg="err",
        hdrs=hdrs,  # type: ignore[arg-type]
        fp=None,
    )


def test_retry_429_honors_retry_after_header(dispatch):
    """A 429 with an integer Retry-After header sleeps that many seconds, then retries."""
    fn = MagicMock(side_effect=[_http_error(429, retry_after="5"), {"ok": True}])
    with patch("time.sleep") as mock_sleep:
        result = dispatch._call_with_retry(fn, max_retries=3)
    assert result == {"ok": True}
    assert fn.call_count == 2
    # min(MAX_BACKOFF_S, 5) == 5
    mock_sleep.assert_called_once_with(5.0)


def test_retry_429_without_header_uses_jittered_backoff(dispatch):
    """A 429 with NO Retry-After header falls back to ADR-0036 jittered backoff."""
    fn = MagicMock(side_effect=[_http_error(429, retry_after=None), {"ok": True}])
    with patch("time.sleep") as mock_sleep:
        result = dispatch._call_with_retry(fn, max_retries=3)
    assert result == {"ok": True}
    assert fn.call_count == 2
    # attempt 0: 2**(0+1) + jitter[0,1) -> [2.0, 3.0)
    (delay,), _ = mock_sleep.call_args
    assert 2.0 <= delay < 3.0


def test_5xx_retries_then_exhaustion_reraises_raw_httperror(dispatch):
    """A persistent 5xx retries with backoff, then re-raises the RAW HTTPError
    (NOT RetryExhaustedError) so downstream raw-HTTPError catchers still fire."""
    err = _http_error(503)
    fn = MagicMock(side_effect=err)
    with patch("time.sleep") as mock_sleep:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            dispatch._call_with_retry(fn, max_retries=3)
    assert excinfo.value is err  # the ORIGINAL exception, not a wrapper
    assert not isinstance(excinfo.value, dispatch.RetryExhaustedError)
    assert fn.call_count == 4  # initial + 3 retries
    assert mock_sleep.call_count == 3


def test_404_fast_fails_raw_no_retry(dispatch):
    """A 404 (absent entity property) is re-raised raw immediately — never retried."""
    err = _http_error(404)
    fn = MagicMock(side_effect=err)
    with patch("time.sleep") as mock_sleep:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            dispatch._call_with_retry(fn, max_retries=3)
    assert excinfo.value is err
    assert fn.call_count == 1
    assert mock_sleep.call_count == 0


def test_acli_rest_raw_httperror_raise_unchanged():
    """No boundary translation: acli_rest's _rest_urlopen_with_retry still re-raises
    the RAW urllib.error.HTTPError (the 8+ raw-HTTPError catchers rely on it)."""
    from rebar_reconciler import acli_rest

    err = _http_error(404)
    # The retry helper is a mixin method needing no instance state — it re-raises
    # HTTPError raw (no retry, no translation).
    client = acli_rest.AcliRestMixin.__new__(acli_rest.AcliRestMixin)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            client._rest_urlopen_with_retry(
                urllib.request.Request("https://example.atlassian.net/x"), timeout=1
            )
    assert excinfo.value is err
    assert not isinstance(excinfo.value, RuntimeError)
