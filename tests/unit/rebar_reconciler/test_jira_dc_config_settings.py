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


# --- AC: resolved_statuses default and custom ---------------------------------------------


def test_resolved_statuses_defaults_to_the_documented_set() -> None:
    """The default the absence-probe classifies against when the config is unset."""
    from rebar_reconciler.adapters.jira_datacenter.settings import DEFAULT_RESOLVED_STATUSES

    assert DEFAULT_RESOLVED_STATUSES == frozenset({"Resolved", "Done", "Cancelled"})


def test_a_custom_workflow_status_set_reaches_the_probe_classifier() -> None:
    """A self-hosted DC workflow can name its resolved states anything.

    Pinning this against the CLASSIFIER rather than the settings tuple is the point: a value
    that is stored but never consulted would satisfy a weaker assertion while leaving the
    probe classifying against the wrong vocabulary.
    """
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport
    from rebar_reconciler.inbound_probe import ProbeBranch

    custom = frozenset({"Shipped", "Abandoned"})
    transport = JiraDataCenterTransport(client=object(), project="X", resolved_statuses=custom)

    from rebar_reconciler.adapters.jira_family import classify_probe_response

    shipped = classify_probe_response(
        "X-1", 200, {"fields": {"status": {"name": "Shipped"}}}, resolved_statuses=custom
    )
    assert shipped.branch is not ProbeBranch.UNREACHABLE
    assert transport._resolved_statuses == custom, (
        "the configured set must reach the transport that classifies with it"
    )
