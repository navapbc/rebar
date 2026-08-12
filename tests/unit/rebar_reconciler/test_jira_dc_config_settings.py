"""The DC connection settings AC(11)-(13) name, which had no tests (story J6, epic e369).

A completion-verification run on ticket 9fd4 refused the close over four criteria. One was a
truncation artifact — ``options["verify"]`` is never False IS covered, by
``test_jira_dc_transport_heldout.py``. The other three were real:

* ``_config_schema._validate_reconciler_tls`` had **zero** test references anywhere in the
  tree, despite being the security control that stops a Jira PAT crossing the wire in
  cleartext;
* ``resolve_jira_datacenter_settings``'s fail-loud property is an ABSENCE — there is no
  ``except ConfigError`` — and an absence is exactly what regresses silently when someone
  later "helpfully" adds a fallback;
* ``resolved_statuses`` appeared in the suite only as a fixture VALUE, never as an assertion
  about its default or a custom workflow's names.

That pattern — a criterion naming a behaviour nothing asserts — is the same one that let
``add_label`` ship calling a method that does not exist on ``jira.JIRA``. These tests close it.
"""

from __future__ import annotations

import logging

import pytest

from rebar._config_schema import ConfigError, _validate_reconciler_tls

pytestmark = pytest.mark.unit


# --- AC: reject non-https unless allow_insecure -------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://jira.internal.example.com",  # a NON-LOOPBACK http URL, as the AC requires
        "http://10.0.0.7:8080/jira",
        "ftp://jira.example.com",
    ],
)
def test_a_non_https_base_url_is_rejected_naming_the_cleartext_risk(base_url: str) -> None:
    """The reject branch. The message must NAME the risk, not just say 'invalid'.

    An operator who sees "not https" may simply force it through; one who is told a Jira PAT
    can be read in transit has the information needed to decide.
    """
    with pytest.raises(ConfigError) as caught:
        _validate_reconciler_tls(base_url, allow_insecure=False)

    message = str(caught.value)
    assert "cleartext" in message, f"the error must name the cleartext risk; got: {message}"
    assert "allow_insecure" in message, (
        f"the error must name the override an operator can set; got: {message}"
    )


def test_the_override_path_warns_and_names_the_risk(caplog: pytest.LogCaptureFixture) -> None:
    """The override branch must not be silent.

    ``allow_insecure = true`` is legitimate for a loopback harness, but a run that silently
    accepts an unencrypted connection gives an operator no signal that it happened.
    """
    with caplog.at_level(logging.WARNING):
        _validate_reconciler_tls("http://localhost:2990/jira", allow_insecure=True)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the override must emit a warning, not pass silently"
    assert any("NOT encrypted" in w for w in warnings), (
        f"the warning must name the risk it is overriding; got: {warnings!r}"
    )


def test_loopback_gets_no_special_case() -> None:
    """``http://localhost`` is rejected too, unless the override is set explicitly.

    This is deliberate and load-bearing: the live harness tests are REQUIRED to set
    ``allow_insecure = true`` so their loopback instance exercises the override path rather
    than slipping through an exemption. A loopback carve-out here would make those tests
    prove nothing about the validator.
    """
    with pytest.raises(ConfigError):
        _validate_reconciler_tls("http://localhost:2990/jira", allow_insecure=False)


def test_https_passes_and_an_unset_url_is_not_validated() -> None:
    """Negative controls, so the validator cannot be satisfied by rejecting everything."""
    _validate_reconciler_tls("https://jira.example.com", allow_insecure=False)
    _validate_reconciler_tls("", allow_insecure=False)  # unset default: nothing to check yet


# --- AC: a malformed config FAILS LOUD rather than degrading to env-only -------------------


def test_a_malformed_config_propagates_rather_than_degrading_to_env_only(monkeypatch) -> None:
    """``resolve_jira_datacenter_settings`` must not swallow ``ConfigError``.

    PR #120 (the hand-rolled client this story replaced) caught it and fell back to env-only
    defaults, turning a typo in ``[tool.rebar.reconciler]`` into a confusing downstream
    connection failure. The fail-loud property here is the ABSENCE of an ``except`` — which no
    test asserted, so adding a well-meaning fallback would have gone unnoticed.
    """
    import rebar.config as _config
    from rebar_reconciler.adapters.jira_datacenter import settings as _settings

    def _raise(*_a, **_k):
        raise ConfigError("reconciler.base_url: malformed value")

    # Patch `rebar.config.load_config`, NOT an attribute on the settings module: the import
    # is FUNCTION-LOCAL (`from rebar.config import load_config` inside the function), so the
    # name is resolved against `rebar.config` at call time and a module-attribute patch here
    # would silently no-op — the test would pass for the wrong reason.
    monkeypatch.setattr(_config, "load_config", _raise)

    with pytest.raises(ConfigError, match="malformed value"):
        _settings.resolve_jira_datacenter_settings()


# --- AC: the retired resolved_statuses plumbing (task 549c) --------------------------------


def test_the_transport_no_longer_carries_a_resolved_statuses_attribute() -> None:
    """Task f020 deleted the inbound absence probe -- the only consumer of the value --
    leaving ``_resolved_statuses`` stored on every transport and read by nothing. Task 549c
    removed that write-only plumbing, so neither the ctor keyword nor the attribute exists.

    Pinned rather than simply deleted: a silent reintroduction would recreate a field that
    looks configurable, is documented as such, and changes nothing."""
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    transport = JiraDataCenterTransport(client=object(), project="X")
    assert not hasattr(transport, "_resolved_statuses")

    with pytest.raises(TypeError):
        JiraDataCenterTransport(  # type: ignore[call-arg]
            client=object(), project="X", resolved_statuses=frozenset({"Shipped"})
        )


def test_settings_no_longer_expose_resolved_statuses() -> None:
    """The NamedTuple member went with the transport plumbing it existed to feed."""
    from rebar_reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings

    assert "resolved_statuses" not in JiraDataCenterSettings._fields


# --- AC(13) sub-requirements the earlier tests did NOT cover -------------------------------
#
# `test_jira_dc_transport_heldout.py` asserts options["verify"] is never False and that a
# ca_bundle lands as a path. A completion-verification run pointed out that the criterion asks
# for two MORE things, and it was right: nothing simulated a verification FAILURE, and nothing
# exercised the `allow_insecure=True` path to show it does not relax certificate verification.
# (I had classified this criterion as already covered — checking that plausibly-named tests
# existed rather than checking them against the criterion's full text.)


def test_a_tls_verification_failure_names_ca_bundle_and_is_not_retried(monkeypatch) -> None:
    """A cert failure must be actionable and must NOT be retried.

    ``requests.exceptions.SSLError`` SUBCLASSES ``requests.exceptions.ConnectionError``, so it
    lands in the transport's retryable set and was re-attempted three times with 2s+5s backoff
    before surfacing as an opaque SSL error. A certificate does not become valid on a retry:
    that is seven wasted seconds, a guaranteed failure, and no mention of the setting that
    fixes it.
    """
    pytest.importorskip("requests")
    from requests.exceptions import SSLError

    from rebar_reconciler.adapters.jira_datacenter import transport as _t

    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise SSLError("certificate verify failed: unable to get local issuer certificate")

    with pytest.raises(_t.TlsVerificationError) as caught:
        _t._with_connection_retry(_boom)

    assert calls["n"] == 1, (
        f"a TLS verification failure must not be retried; it ran {calls['n']} times"
    )
    message = str(caught.value)
    assert "ca_bundle" in message, f"the error must name the setting that fixes it; got: {message}"
    assert "allow_insecure" in message, (
        "and must say allow_insecure does NOT govern certificate verification, since that is "
        f"the natural wrong guess; got: {message}"
    )


def test_allow_insecure_does_not_relax_certificate_verification(monkeypatch) -> None:
    """``allow_insecure`` governs the URL SCHEME check only.

    Without this, an operator could reasonably read `allow_insecure = true` as "skip TLS
    checks" — and nothing asserted otherwise. The live harness sets exactly this flag, so if it
    did relax verification the harness would be silently exercising an unverified path.
    """
    from rebar_reconciler.adapters.jira_datacenter import transport as _t
    from rebar_reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings

    captured: dict = {}

    class _Spy:
        def __init__(self, *a, **k) -> None:
            captured.update(k)

    settings = JiraDataCenterSettings(
        url="http://localhost:2990/jira",
        project="DC",
        allow_insecure=True,  # the override under test
        ca_bundle="",
        pat="pat-xyz",
    )
    monkeypatch.setattr(_t, "_jira_client_class", lambda: _Spy, raising=False)
    _t.build_client_from_settings(settings)

    options = captured.get("options") or {}
    assert options.get("verify", True) is not False, (
        f"allow_insecure must NOT disable certificate verification; got options={options!r}"
    )
    assert captured.get("verify") is not False, "and not via a bare kwarg either"
