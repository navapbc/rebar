"""Require Jira Data Center PAT authentication to fail closed (bug cd78).

A missing ``JIRA_PAT`` must raise an error naming the variable before any network call,
never construct an anonymous client that can misreport permission failures as missing
projects or silently read a partial view. The guard belongs at client construction, not
settings resolution: runtime-checkable Protocol probes may evaluate properties on Python
3.11. Settings therefore remain total for Protocol checks while construction enforces the
security boundary.
"""

from __future__ import annotations

import pytest

from rebar_reconciler._backend import BackendEnvError
from rebar_reconciler.adapters.jira_datacenter import settings as dc_settings
from rebar_reconciler.adapters.jira_datacenter import transport as dc_transport


def _settings(pat: str) -> dc_settings.JiraDataCenterSettings:
    return dc_settings.JiraDataCenterSettings(
        url="https://jira.example.gov",
        project="REB",
        allow_insecure=False,
        ca_bundle="",
        pat=pat,
    )


@pytest.mark.parametrize(
    ("pat", "label"),
    [("", "empty"), ("   ", "whitespace-only"), ("\t\n", "blank")],
    ids=["empty", "whitespace", "blank"],
)
def test_a_missing_or_blank_pat_fails_naming_the_variable(monkeypatch, pat, label) -> None:
    """THE BUG. Each of these previously produced a client carrying an empty bearer token.
    An accidental `export JIRA_PAT=` is the same mistake as forgetting it entirely, so the
    blank forms are rejected too."""

    def _must_not_be_called():  # pragma: no cover - the assertion is that it is not
        raise AssertionError("a Jira client class was resolved despite a missing JIRA_PAT")

    monkeypatch.setattr(dc_transport, "_jira_client_class", _must_not_be_called)

    with pytest.raises(BackendEnvError) as excinfo:
        dc_transport.build_client_from_settings(_settings(pat))

    message = str(excinfo.value)
    assert "JIRA_PAT" in message, (
        f"the {label} case failed without naming JIRA_PAT, so an operator cannot tell what "
        f"is missing: {message!r}"
    )
    assert "anonymous" in message.lower(), (
        "the error should name the anonymous-fallback consequence — that is what makes the "
        f"misleading 'project does not exist' symptom intelligible: {message!r}"
    )


def test_the_guard_fires_before_any_client_is_constructed(monkeypatch) -> None:
    """TEETH. An auth failure surfacing later from the server is NOT equivalent: on a
    permissive instance anonymous access succeeds and the pass silently reads a partial
    view. Nothing may reach the network."""
    calls: list[int] = []
    monkeypatch.setattr(dc_transport, "_jira_client_class", lambda: calls.append(1))

    with pytest.raises(BackendEnvError):
        dc_transport.build_client_from_settings(_settings(""))
    assert not calls, "a client class was resolved despite the guard"


def test_a_real_pat_builds_a_client(monkeypatch) -> None:
    """The guard must not break the working path."""
    built: dict = {}

    def _fake_cls():
        def _ctor(server, token_auth, options):
            built.update(server=server, token_auth=token_auth)
            return object()

        return _ctor

    monkeypatch.setattr(dc_transport, "_jira_client_class", _fake_cls)
    dc_transport.build_client_from_settings(_settings("a-real-token"))
    assert built["token_auth"] == "a-real-token"
    assert built["server"] == "https://jira.example.gov"


def test_settings_resolution_stays_total_for_protocol_checks(monkeypatch) -> None:
    """REGRESSION GUARD for the trap this fix fell into once.

    `JiraDataCenterBackend.query_project` calls `resolve_jira_datacenter_settings`, and on
    Python <= 3.11 a runtime-checkable Protocol `isinstance` evaluates properties via
    `hasattr`. So a raise inside resolution breaks `isinstance(backend, Backend)` — on 3.11
    only, which means it passes locally on 3.12+ and fails in CI. Resolution must therefore
    stay TOTAL: it may not raise merely because a credential is absent.

    Asserted the way 3.11 would exercise it — `hasattr` on the property — so the guarantee
    holds regardless of the interpreter running this test.
    """
    monkeypatch.delenv("JIRA_PAT", raising=False)

    from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    class _FakeTransport:
        project = "REB"

    backend = JiraDataCenterBackend(transport=_FakeTransport())

    # The exact operation Python 3.11's Protocol check performs. It must not raise, and it
    # must not report the attribute missing.
    assert hasattr(backend, "query_project"), (
        "query_project raised with JIRA_PAT unset — settings resolution is no longer total, "
        "so isinstance(backend, Backend) will FAIL on Python 3.11 while passing on 3.12+"
    )
