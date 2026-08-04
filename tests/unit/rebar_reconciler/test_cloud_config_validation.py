"""Cloud config-validation coverage — story 2127, item 2 (item 2a now ENFORCED by bdb8).

  behaviour                 | DC (has it)                              | Cloud
  --------------------------+------------------------------------------+-----------------------
  non-https url rejected     | ReconcilerConfig.__post_init__ calls     | JiraConfig.__post_init__
  (item 2a — IMPLEMENTED)    | _validate_reconciler_tls → ConfigError   | now rejects too, via the
                            | (_config_schema.py)                      | shared _validate_https_url
                            |                                          | (bug bdb8)
  missing credential        | (DC PAT is required by its transport)    | _build_jira_backend
  fails loudly (item 2b —   |                                          | builds an AcliClient
  still ABSENCE-DOCUMENTED)  |                                          | with EMPTY creds, no
                            |                                          | loud failure

Item 2a was originally absence-documenting; bug bdb8-0646-9e13-4bfb closed the gap, so the
tests below now PIN the ENFORCEMENT (JiraConfig rejects a non-https url, with a
``jira.allow_insecure`` override) — the mutation-check is the inverse: removing
``JiraConfig.__post_init__`` flips these RED.

Item 2b remains ABSENCE-DOCUMENTING (the current permissive behaviour), tracked by the
follow-up bug cited below; its inverse mutation-check (add a credential guard → the
absence-doc assertion flips) is recorded in ad85's change.

Follow-up bugs (filed + linked ``discovered_from`` 2127):
  * Cloud non-https URL not rejected  → bug bdb8-0646-9e13-4bfb  (CLOSED by this change)
  * Cloud missing-credential silent   → bug ad85-e5e3-be8d-4be5  (still open)
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
# Item 2b — missing credential. Cloud's backend factory proceeds SILENTLY
# (absence-documenting), building an AcliClient with empty creds.
# ---------------------------------------------------------------------------


def test_cloud_build_backend_with_missing_credentials_does_not_fail_loudly() -> None:
    """ABSENCE-DOC: ``_build_jira_backend`` resolves settings via
    ``resolve_jira_settings`` (which returns ``""`` for a missing url/user/token) and
    constructs the ``AcliClient`` transport WITHOUT asserting the credentials are present
    — so a wholly-unconfigured Cloud environment builds a live-looking backend that will
    only fail later, at the first real API call, instead of failing loudly at
    construction. Pinned as current behaviour; tracked by the follow-up bug in the module
    docstring.

    MUTATION-CHECK (inverse): add a ``if not (s.url and s.user and s.api_token): raise``
    guard to ``_build_jira_backend`` → this test's ``does not raise`` expectation flips to
    RED, proving it measures the absence of the guard.
    """
    from rebar_reconciler.adapters.jira import acli_subprocess, backend

    empty = acli_subprocess.JiraSettings(url="", user="", project="", api_token="")

    import rebar_reconciler.adapters.jira.acli_subprocess as acli_subprocess_mod

    orig = acli_subprocess_mod.resolve_jira_settings
    try:
        acli_subprocess_mod.resolve_jira_settings = lambda **_k: empty  # type: ignore[assignment]
        # Must NOT raise despite wholly-absent credentials — the documented gap.
        built = backend._build_jira_backend(config=_DummyConfig())
    finally:
        acli_subprocess_mod.resolve_jira_settings = orig  # type: ignore[assignment]

    # A backend was constructed; its transport carries the empty credentials verbatim.
    assert built.transport.jira_url == ""
    assert built.transport.user == ""
    assert built.transport.api_token == ""


class _DummyConfig:
    """Minimal stand-in — ``_build_jira_backend`` ignores its argument and resolves
    settings through ``resolve_jira_settings`` (which we stub above)."""

    jira: Any = None
