"""Cloud config-validation coverage — story 2127, item 2 (2a ENFORCED by bdb8; 2b by ad85).

Two Cloud config-validation behaviours the DC transport HAS, BOTH now at parity: item 2a
(non-https URL) is ENFORCED (bug bdb8) and item 2b (missing credential) is ENFORCED (bug
ad85) — ``_build_jira_backend`` fails loudly at construction. Each has an inverse
mutation-check recorded RED/GREEN in its change description.

  behaviour                 | DC (has it)                              | Cloud
  --------------------------+------------------------------------------+-----------------------
  non-https base URL        | ReconcilerConfig.__post_init__ calls     | JiraConfig.__post_init__
  rejected                  | _validate_reconciler_tls → ConfigError   | now rejects too, via the
                            | (_config_schema.py)                      | shared _validate_https_url
                            |                                          | — ENFORCED (bug bdb8)
  missing/invalid credential| (DC PAT is required by its transport)    | _build_jira_backend
  fails loudly              |                                          | raises BackendEnvError
                            |                                          | at construction —
                            |                                          | ENFORCED (bug ad85)

Item 2a was originally absence-documenting; bug bdb8-0646-9e13-4bfb closed the gap, so the
tests below now PIN the ENFORCEMENT (JiraConfig rejects a non-https url, with a
``jira.allow_insecure`` override) — the mutation-check is the inverse: removing
``JiraConfig.__post_init__`` flips these RED.

Item 2b is now ENFORCED (bug ad85): the tests below PIN the loud failure (``_build_jira_backend``
raises ``BackendEnvError`` on a missing/blank/non-email credential); its inverse mutation-check
(remove the credential guard → the enforcement assertions flip RED) is recorded in ad85's change.

Follow-up bugs (filed + linked ``discovered_from`` 2127):
  * Cloud non-https URL not rejected  → bug bdb8-0646-9e13-4bfb  (RESOLVED — enforced above)
  * Cloud missing-credential silent   → bug ad85-e5e3-be8d-4be5  (RESOLVED — enforced below)
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from rebar._config_schema import InsecureUrlError, JiraConfig, ReconcilerConfig
from rebar.config import ConfigError

# ---------------------------------------------------------------------------
# Item 2a — non-https url. DC rejects; Cloud now rejects too (bug bdb8).
# ---------------------------------------------------------------------------


def test_dc_reconciler_rejects_non_https_base_url() -> None:
    """CONTRAST anchor: the DC ReconcilerConfig rejects a cleartext base_url. Cloud's
    JiraConfig below is now measured to match it."""
    with pytest.raises(ConfigError, match="not 'https'"):
        ReconcilerConfig(base_url="http://jira.internal")


def test_dc_reconciler_allows_non_https_when_allow_insecure() -> None:
    """The DC override path: allow_insecure=true downgrades the rejection to a warning."""
    cfg = ReconcilerConfig(base_url="http://jira.internal", allow_insecure=True)
    assert cfg.base_url == "http://jira.internal"


def test_cloud_jira_config_rejects_non_https_url() -> None:
    """ENFORCEMENT (bug bdb8): Cloud's ``JiraConfig`` now rejects a cleartext ``http://``
    url with a ``ConfigError`` (an ``InsecureUrlError``), mirroring the DC
    ``ReconcilerConfig``. The message must NAME the cleartext risk and the override key so
    an operator knows how to proceed.

    MUTATION-CHECK (inverse): remove ``JiraConfig.__post_init__`` → construction stops
    raising and this test goes RED, proving it measures the enforcement.
    """
    with pytest.raises(InsecureUrlError) as caught:
        JiraConfig(url="http://insecure.example.com", user="u", project="DIG")
    message = str(caught.value)
    assert "not 'https'" in message
    assert "cleartext" in message
    assert "jira.allow_insecure" in message
    # A subclass of ConfigError, so existing ``except ConfigError`` handlers still catch it.
    assert isinstance(caught.value, ConfigError)


def test_cloud_jira_config_allows_non_https_when_allow_insecure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The override path (parity with the DC one): ``jira.allow_insecure=true`` downgrades
    the rejection to a warning that names the risk it is overriding."""
    with caplog.at_level(logging.WARNING):
        cfg = JiraConfig(url="http://insecure.example.com", allow_insecure=True)
    assert cfg.url == "http://insecure.example.com"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("NOT encrypted" in w for w in warnings), (
        f"the override must warn, naming the risk; got: {warnings!r}"
    )


def test_cloud_jira_config_accepts_https_and_empty_url() -> None:
    """Negative controls: an https url and the unset default are accepted (so the
    validator cannot be satisfied by rejecting everything)."""
    assert JiraConfig(url="https://secure.example.com", user="u", project="DIG").url == (
        "https://secure.example.com"
    )
    assert JiraConfig().url == ""  # unset default: nothing to validate yet


# ---------------------------------------------------------------------------
# Item 2b — missing credential. ENFORCED (bug ad85): Cloud's backend factory now
# fails LOUDLY at construction (parity with DC's JIRA_PAT guard,
# test_dc_pat_required_heldout.py) instead of silently building a doomed AcliClient
# with empty creds. Research-approved shape: presence + minimal email-format check,
# no live network probe.
# ---------------------------------------------------------------------------


def _stub_settings(**over: Any):
    from rebar_reconciler.adapters.jira import acli_subprocess

    base = {
        "url": "https://acme.atlassian.net",
        "user": "you@example.com",
        "project": "DIG",
        "api_token": "tok",
    }
    base.update(over)
    return acli_subprocess.JiraSettings(**base)


def _build_with_settings(monkeypatch, settings, *, forbid_client: bool = False):
    """Drive ``_build_jira_backend`` with a stubbed ``resolve_jira_settings``. When
    ``forbid_client`` is set, ``AcliClient`` is replaced with a sentinel that fails if
    constructed — proving the credential guard runs BEFORE transport construction."""
    from rebar_reconciler.adapters.jira import acli, acli_subprocess, backend

    monkeypatch.setattr(acli_subprocess, "resolve_jira_settings", lambda **_k: settings)
    if forbid_client:

        def _never(**_k):
            raise AssertionError("AcliClient was constructed — the guard did not run first")

        monkeypatch.setattr(acli, "AcliClient", _never)
    return backend._build_jira_backend(config=_DummyConfig())


@pytest.mark.parametrize("field", ["url", "user", "api_token"])
@pytest.mark.parametrize("bad", ["", "   ", "\t\n"], ids=["empty", "whitespace", "blank"])
def test_cloud_build_backend_missing_credential_fails_loudly(monkeypatch, field, bad) -> None:
    """ENFORCEMENT (bug ad85): a missing OR whitespace-only ``JIRA_URL`` / ``JIRA_USER`` /
    ``JIRA_API_TOKEN`` makes ``_build_jira_backend`` raise ``BackendEnvError`` at
    construction — naming the missing var and the anonymous-access consequence — instead of
    building an ``AcliClient`` with empty creds that only fails at the first API call. The
    guard runs BEFORE the transport is constructed (``forbid_client``)."""
    from rebar_reconciler._backend import BackendEnvError

    env_names = {"url": "JIRA_URL", "user": "JIRA_USER", "api_token": "JIRA_API_TOKEN"}
    settings = _stub_settings(**{field: bad})
    with pytest.raises(BackendEnvError) as excinfo:
        _build_with_settings(monkeypatch, settings, forbid_client=True)
    message = str(excinfo.value)
    assert env_names[field] in message, f"error must name the missing var: {message!r}"
    assert "anonymous" in message.lower(), f"error must name the anonymous fallback: {message!r}"


def test_cloud_build_backend_names_every_missing_credential(monkeypatch) -> None:
    """All three absent → ONE message names ALL of them (no fix-one-rerun loop), mirroring
    DC's single-message guard."""
    from rebar_reconciler._backend import BackendEnvError

    settings = _stub_settings(url="", user="", api_token="")
    with pytest.raises(BackendEnvError) as excinfo:
        _build_with_settings(monkeypatch, settings, forbid_client=True)
    message = str(excinfo.value)
    for name in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"):
        assert name in message, f"{name} not named in {message!r}"


@pytest.mark.parametrize("user", ["admin", "not-an-email", "@no-local.example", "no-domain@"])
def test_cloud_build_backend_non_email_user_fails_loudly(monkeypatch, user) -> None:
    """Cloud authenticates with the Atlassian account EMAIL as the Basic-auth username
    (jira.js's hard-won lesson); a bare handle/accountId silently 401s. A present-but-not-
    an-email ``JIRA_USER`` raises ``BackendEnvError`` naming ``JIRA_USER``."""
    from rebar_reconciler._backend import BackendEnvError

    settings = _stub_settings(user=user)
    with pytest.raises(BackendEnvError) as excinfo:
        _build_with_settings(monkeypatch, settings, forbid_client=True)
    assert "JIRA_USER" in str(excinfo.value)


@pytest.mark.parametrize(
    "user",
    ["you@example.com", "a.b+tag@sub.example.com", "First.Last@team.atlassian.net"],
)
def test_cloud_build_backend_accepts_valid_email(monkeypatch, user) -> None:
    """A valid account email — including ``+tag`` subaddressing and subdomains — is
    ACCEPTED. Guards against an over-strict email check that rejects valid addresses (the
    documented email-validation trap)."""
    built = _build_with_settings(monkeypatch, _stub_settings(user=user))
    assert built.transport.user == user
    assert built.transport.api_token == "tok"


class _DummyConfig:
    """Minimal stand-in — ``_build_jira_backend`` ignores its argument and resolves
    settings through ``resolve_jira_settings`` (which we stub above)."""

    jira: Any = None
