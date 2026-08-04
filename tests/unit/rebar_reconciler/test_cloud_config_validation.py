"""Cloud config-validation coverage — story 2127, item 2.

Two Cloud config-validation behaviours the DC transport HAS and Cloud LACKS. Both tests
are ABSENCE-DOCUMENTING: they pin the CURRENT (permissive) Cloud behaviour and cite the
follow-up bug that tracks closing the gap, so the divergence is a recorded decision rather
than an undetected hole. Each has an inverse mutation-check (add the missing guard → the
absence-doc assertion flips), recorded RED/GREEN in the change description.

  behaviour                 | DC (has it)                              | Cloud (lacks it)
  --------------------------+------------------------------------------+-----------------------
  non-https base URL        | ReconcilerConfig.__post_init__ calls     | JiraConfig has NO
  rejected                  | _validate_reconciler_tls → ConfigError   | __post_init__; url is
                            | (_config_schema.py)                      | stored unvalidated
  missing credential        | (DC PAT is required by its transport)    | _build_jira_backend
  fails loudly              |                                          | builds an AcliClient
                            |                                          | with EMPTY creds, no
                            |                                          | loud failure

Follow-up bugs (filed + linked ``discovered_from`` 2127):
  * Cloud non-https URL not rejected  → bug bdb8-0646-9e13-4bfb
  * Cloud missing-credential silent   → bug ad85-e5e3-be8d-4be5
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar._config_schema import JiraConfig, ReconcilerConfig
from rebar.config import ConfigError

# ---------------------------------------------------------------------------
# Item 2a — non-https base URL. DC rejects; Cloud does NOT (absence-documenting).
# ---------------------------------------------------------------------------


def test_dc_reconciler_rejects_non_https_base_url() -> None:
    """CONTRAST anchor: the DC ReconcilerConfig DOES reject a cleartext base_url. This is
    the behaviour Cloud is measured against — if this ever stops raising, the Cloud
    absence-doc below is comparing against nothing."""
    with pytest.raises(ConfigError, match="not 'https'"):
        ReconcilerConfig(base_url="http://jira.internal")


def test_dc_reconciler_allows_non_https_when_allow_insecure() -> None:
    """The DC override path: allow_insecure=true downgrades the rejection to a warning."""
    cfg = ReconcilerConfig(base_url="http://jira.internal", allow_insecure=True)
    assert cfg.base_url == "http://jira.internal"


def test_cloud_jira_config_does_not_reject_non_https_url_absence_documented() -> None:
    """ABSENCE-DOC: Cloud's ``JiraConfig`` has no ``__post_init__`` and performs NO
    URL-scheme validation, so a cleartext ``http://`` Jira URL is accepted silently —
    unlike the DC ``ReconcilerConfig``. Pinned as the current behaviour; the gap is
    tracked by the follow-up bug cited in the module docstring.

    MUTATION-CHECK (inverse): give ``JiraConfig`` a ``__post_init__`` that rejects a
    non-https ``url`` → this test flips to expecting ``ConfigError`` and the assertion
    below goes RED, proving it actually measures the absence.
    """
    cfg = JiraConfig(url="http://insecure.example.com", user="u", project="DIG")
    # No exception, no normalisation — the cleartext scheme is retained verbatim.
    assert cfg.url == "http://insecure.example.com"
    # JiraConfig is a plain dataclass — it defines no validating __post_init__ hook of its
    # own (the mechanism by which the DC ReconcilerConfig rejects a non-https URL).
    assert "__post_init__" not in vars(JiraConfig)


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
