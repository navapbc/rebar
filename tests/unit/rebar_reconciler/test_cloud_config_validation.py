"""Cloud config-validation coverage — story 2127, item 2.

Two Cloud config-validation behaviours the DC transport HAS. Item 2a (non-https URL) is
still ABSENCE-DOCUMENTING here; item 2b (missing credential) is now ENFORCED (bug ad85):
``_build_jira_backend`` fails loudly at construction, at parity with DC. The absence-doc
tests pin the CURRENT behaviour and cite the follow-up bug; each has an inverse
mutation-check recorded RED/GREEN in the change description.

  behaviour                 | DC (has it)                              | Cloud
  --------------------------+------------------------------------------+-----------------------
  non-https base URL        | ReconcilerConfig.__post_init__ calls     | JiraConfig has NO
  rejected                  | _validate_reconciler_tls → ConfigError   | __post_init__; url is
                            | (_config_schema.py)                      | stored unvalidated
                            |                                          | (absence-doc; bdb8)
  missing/invalid credential| (DC PAT is required by its transport)    | _build_jira_backend
  fails loudly              |                                          | raises BackendEnvError
                            |                                          | at construction —
                            |                                          | ENFORCED (bug ad85)

Follow-up bugs (filed + linked ``discovered_from`` 2127):
  * Cloud non-https URL not rejected  → bug bdb8-0646-9e13-4bfb (absence-doc below)
  * Cloud missing-credential silent   → bug ad85-e5e3-be8d-4be5 (RESOLVED — enforced below)
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
