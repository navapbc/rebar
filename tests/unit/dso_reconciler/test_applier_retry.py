"""Tests for _call_with_retry in dso_reconciler/applier.py.

Covers:
- RetryExhaustedError raised after max_retries on TimeoutError
- Success on 2nd attempt (transient 503 simulated)
- Fail fast on 404 (non-retryable 4xx)
- Retry on 429 (rate-limit, retryable 4xx)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_retry_test", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_retry_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    """Load the applier module, failing all tests if absent."""
    if not APPLIER_PATH.exists():
        pytest.fail(
            f"applier.py not found at {APPLIER_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_applier()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_retry_exhausted_on_timeout(applier):
    """_call_with_retry raises RetryExhaustedError after max_retries on TimeoutError."""
    fn = MagicMock(side_effect=TimeoutError("hung call"))

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(applier.RetryExhaustedError):
            applier._call_with_retry(fn, max_retries=3)

    # fn called on initial attempt + 3 retries = 4 total
    assert fn.call_count == 4
    # sleep called 3 times with exponential delays
    assert mock_sleep.call_count == 3
    mock_sleep.assert_any_call(1)
    mock_sleep.assert_any_call(2)
    mock_sleep.assert_any_call(4)


def test_success_on_second_attempt(applier):
    """_call_with_retry succeeds on 2nd attempt when first raises a transient 503."""
    JiraAPIError = applier.JiraAPIError
    fn = MagicMock(
        side_effect=[JiraAPIError("service unavailable", 503), {"key": "PROJ-1"}]
    )

    with patch("time.sleep") as mock_sleep:
        result = applier._call_with_retry(fn, max_retries=3)

    assert result == {"key": "PROJ-1"}
    assert fn.call_count == 2
    # One sleep between attempt 0 and attempt 1
    mock_sleep.assert_called_once_with(1)


def test_fail_fast_on_404(applier):
    """_call_with_retry re-raises immediately on non-retryable 404."""
    JiraAPIError = applier.JiraAPIError
    fn = MagicMock(side_effect=JiraAPIError("not found", 404))

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(JiraAPIError) as exc_info:
            applier._call_with_retry(fn, max_retries=3)

    assert exc_info.value.status_code == 404
    # Only one call; no retries on non-retryable error
    assert fn.call_count == 1
    mock_sleep.assert_not_called()


def test_retry_on_429(applier):
    """_call_with_retry retries on 429 (rate limit) and succeeds after backoff."""
    JiraAPIError = applier.JiraAPIError
    fn = MagicMock(
        side_effect=[
            JiraAPIError("rate limited", 429),
            JiraAPIError("rate limited", 429),
            {"key": "PROJ-2"},
        ]
    )

    with patch("time.sleep") as mock_sleep:
        result = applier._call_with_retry(fn, max_retries=3)

    assert result == {"key": "PROJ-2"}
    assert fn.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(1)
    mock_sleep.assert_any_call(2)
