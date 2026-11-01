"""BackendHTTPError must subclass urllib HTTPError for adapter-neutral handling.

The Data Center adapter wraps vendor failures so the core classifies 404 as
deleted and other read failures as transport errors. Reporter 4xx failures
still degrade softly, while Cloud classification remains unchanged. Drive
those core paths, not only an isinstance check."""

from __future__ import annotations

import urllib.error

import pytest

from rebar.reconciler import dispatch_apply_phases, outbound_differ


def _dc_error(status: int):
    """The error a DC transport call raises after translation."""
    from rebar.reconciler._backend import BackendHTTPError

    return BackendHTTPError(
        url="http://jira.example/rest/api/2/issue/DC-1",
        code=status,
        msg=f"DC returned {status}",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


class _RaisingClient:
    """A transport whose read fails the way a real DC one would."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get_issue_by_rest(self, jira_key: str):
        raise self._exc

    def set_reporter(self, issue_key: str, account_id: str) -> None:
        raise self._exc


# ---------------------------------------------------------------------------
# The contract type itself
# ---------------------------------------------------------------------------


def test_backend_http_error_is_declared_on_the_port() -> None:
    from rebar.reconciler import _backend

    assert hasattr(_backend, "BackendHTTPError"), (
        "the port must DECLARE the transport error contract — a backend that has to guess "
        "which exception the core catches is the defect this story fixes"
    )


def test_backend_http_error_subclasses_urllib_http_error() -> None:
    """BackendHTTPError remains a urllib HTTPError subtype so existing core catches still apply."""
    from rebar.reconciler._backend import BackendHTTPError

    assert issubclass(BackendHTTPError, urllib.error.HTTPError)


def test_backend_http_error_carries_the_status_code() -> None:
    assert _dc_error(404).code == 404
    assert _dc_error(403).code == 403


# ---------------------------------------------------------------------------
# Deletion classification — driven through the REAL function
# ---------------------------------------------------------------------------


def test_a_dc_404_is_classified_as_DELETED_by_the_real_differ() -> None:
    """The outbound differ classifies a Data Center 404 as deleted."""
    result = outbound_differ._safe_get_issue(_RaisingClient(_dc_error(404)), "DC-1")
    assert result is outbound_differ._DELETED


def test_a_dc_non_404_is_a_TRANSPORT_ERROR_not_a_DELETE() -> None:
    """A Data Center non-404 read failure is a transport error, never deletion."""
    for status in (403, 500, 503):
        result = outbound_differ._safe_get_issue(_RaisingClient(_dc_error(status)), "DC-1")
        assert result is outbound_differ._TRANSPORT_ERROR, f"status {status} misclassified"


def test_cloud_classification_is_unchanged() -> None:
    """Cloud 404 and other HTTP failures retain their pre-adapter classifications."""
    cloud_404 = urllib.error.HTTPError("http://x", 404, "gone", None, None)  # type: ignore[arg-type]
    cloud_500 = urllib.error.HTTPError("http://x", 500, "boom", None, None)  # type: ignore[arg-type]
    assert outbound_differ._safe_get_issue(_RaisingClient(cloud_404), "DIG-1") is (
        outbound_differ._DELETED
    )
    assert outbound_differ._safe_get_issue(_RaisingClient(cloud_500), "DIG-1") is (
        outbound_differ._TRANSPORT_ERROR
    )


# ---------------------------------------------------------------------------
# Reporter soft-degradation — driven through the REAL phase function
# ---------------------------------------------------------------------------


def test_a_dc_reporter_4xx_degrades_softly_instead_of_escaping(monkeypatch) -> None:
    """A Data Center reporter 4xx degrades through the core handler instead of escaping."""
    recorded: list[str] = []
    monkeypatch.setattr(
        dispatch_apply_phases,
        "_record_reporter_alert",
        lambda kind, jira_key, reason: recorded.append(kind),
        raising=True,
    )
    monkeypatch.setattr(
        dispatch_apply_phases, "_jira_account_id_for", lambda ref: "dcuser", raising=True
    )

    fields = {"reporter": "someone"}
    # Must NOT raise — the whole point of the handler.
    dispatch_apply_phases._update_one_apply_reporter(fields, "DC-1", _RaisingClient(_dc_error(403)))
    assert "outbound-reporter-not-permitted" in recorded, (
        f"the DC 4xx was not caught by the existing soft-degradation handler: {recorded}"
    )


def test_reporter_handler_still_degrades_for_cloud(monkeypatch) -> None:
    """NEGATIVE CONTROL for the same path."""
    recorded: list[str] = []
    monkeypatch.setattr(
        dispatch_apply_phases,
        "_record_reporter_alert",
        lambda kind, jira_key, reason: recorded.append(kind),
        raising=True,
    )
    monkeypatch.setattr(
        dispatch_apply_phases, "_jira_account_id_for", lambda ref: "acct-1", raising=True
    )
    cloud_403 = urllib.error.HTTPError("http://x", 403, "denied", None, None)  # type: ignore[arg-type]
    dispatch_apply_phases._update_one_apply_reporter(
        {"reporter": "someone"}, "DIG-1", _RaisingClient(cloud_403)
    )
    assert "outbound-reporter-not-permitted" in recorded


# ---------------------------------------------------------------------------
# The core stays vendor-neutral — structural
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module", ["outbound_differ.py", "dispatch_apply_phases.py", "outbound_field_diff.py"]
)
def test_no_core_module_names_a_vendor_exception(module: str) -> None:
    """Core reconciler modules use neutral transport exceptions rather than Jira vendor types."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "src/rebar/reconciler"
    text = (root / module).read_text()
    assert "JIRAError" not in text, (
        f"{module} names a vendor-specific exception — translate at the adapter boundary "
        f"instead, or the core grows one except-clause per backend"
    )
