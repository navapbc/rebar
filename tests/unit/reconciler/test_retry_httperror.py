"""Keep the REST floor's raw HTTPError boundary after ADR 0103 S3.

For a nonretryable 404, acli_rest re-raises the original HTTPError. Downstream
catchers rely on that identity. The retired handler wrapper must not add a
second retry layer around the floor."""

from __future__ import annotations

import email.message
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest


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


def test_acli_rest_raw_httperror_raise_unchanged():
    """No boundary translation: acli_rest's _rest_urlopen_with_retry still re-raises
    the RAW urllib.error.HTTPError (the 8+ raw-HTTPError catchers rely on it)."""
    from rebar.reconciler.adapters.jira import acli_rest

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
