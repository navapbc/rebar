"""HELD-OUT: a missing JIRA_PAT must fail closed, not fall back to anonymous (bug cd78).

`docs/user-guide.md` promises this as a SECURITY property of the DC backend:

    a missing `JIRA_PAT` fails with an error naming the variable rather than falling back
    to anonymous access

It did not. `resolve_jira_datacenter_settings` read `os.environ.get("JIRA_PAT", "")` and
passed the empty string straight to `token_auth=`, so the client constructed happily and
every request went out unauthenticated. Jira then answered with
`The value 'RBJ…' does not exist for the field 'project'` — the message it deliberately
returns for a project the caller cannot BROWSE, so as not to leak existence.

Two reasons that failure mode is worse than a plain error:

* it MISATTRIBUTES in the most expensive direction — an operator who forgot the export is
  told their PROJECT is wrong, and goes off checking project keys, permissions and JQL;
* on an instance where anonymous CAN browse, there is no error at all: the pass reads a
  partial or empty view and reports success. That is the "green but broken" shape this epic
  keeps finding, and it is why the guard is asserted to run BEFORE any network call rather
  than being left to surface as an auth error later.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.adapters.jira_datacenter import settings as dc_settings


@pytest.fixture
def _dc_config(monkeypatch, tmp_path):
    """A minimal valid DC config so the PAT is the only variable under test."""

    class _Reconciler:
        base_url = "https://jira.example.gov"
        allow_insecure = False
        ca_bundle = ""
        resolved_statuses = None

    class _Jira:
        project = "REB"

    class _Config:
        reconciler = _Reconciler()
        jira = _Jira()

    import rebar.config as _cfg

    monkeypatch.setattr(_cfg, "load_config", lambda *a, **k: _Config())
    return _Config


@pytest.mark.parametrize(
    ("env_value", "label"),
    [(None, "unset"), ("", "empty"), ("   ", "whitespace-only"), ("\t\n", "blank")],
    ids=["unset", "empty", "whitespace", "blank"],
)
def test_a_missing_or_blank_pat_fails_naming_the_variable(
    monkeypatch, _dc_config, env_value, label
) -> None:
    """THE BUG. Every one of these previously resolved to a settings object carrying an
    empty bearer token. An accidental `export JIRA_PAT=` is the same mistake as forgetting
    it entirely, so the blank forms are rejected too rather than only the unset one."""
    if env_value is None:
        monkeypatch.delenv("JIRA_PAT", raising=False)
    else:
        monkeypatch.setenv("JIRA_PAT", env_value)

    with pytest.raises(Exception) as excinfo:
        dc_settings.resolve_jira_datacenter_settings()

    message = str(excinfo.value)
    assert "JIRA_PAT" in message, (
        f"the {label} case failed without naming JIRA_PAT, so an operator cannot tell what "
        f"is missing: {message!r}"
    )
    assert "environment" in message.lower(), (
        "the error should say the credential is environment-only — putting it in the config "
        f"file is the most likely mistake and the guide forbids it: {message!r}"
    )


def test_the_guard_runs_before_any_client_is_built(monkeypatch, _dc_config) -> None:
    """TEETH. An auth failure surfacing later from the server is NOT equivalent: on a
    permissive instance anonymous access succeeds and the pass silently reads a partial
    view. The invariant is that resolution fails before anything reaches the network."""
    monkeypatch.delenv("JIRA_PAT", raising=False)

    from rebar_reconciler.adapters.jira_datacenter import transport as dc_transport

    def _must_not_be_called(*a, **k):  # pragma: no cover - the assertion is that it is not
        raise AssertionError(
            "a Jira client was constructed despite a missing JIRA_PAT — the guard runs too "
            "late, so an anonymous request can still reach the server"
        )

    monkeypatch.setattr(dc_transport, "_jira_client_class", _must_not_be_called)

    with pytest.raises(Exception) as excinfo:
        dc_settings.resolve_jira_datacenter_settings()
    assert "JIRA_PAT" in str(excinfo.value)


def test_a_real_pat_resolves_normally(monkeypatch, _dc_config) -> None:
    """The guard must not break the working path."""
    monkeypatch.setenv("JIRA_PAT", "a-real-token")
    settings = dc_settings.resolve_jira_datacenter_settings()
    assert settings.pat == "a-real-token"
    assert settings.project == "REB"


def test_a_pat_in_config_does_not_satisfy_the_guard(monkeypatch, _dc_config) -> None:
    """The env-only contract the guide promises. A PAT sitting in a committed config file
    must NOT be accepted — that is the whole point of the credential being env-only, and it
    is exactly what the live test simulates."""
    monkeypatch.delenv("JIRA_PAT", raising=False)

    class _ReconcilerWithPat(_dc_config.reconciler.__class__):
        jira_pat = "sneaked-in-via-config"

    monkeypatch.setattr(_dc_config, "reconciler", _ReconcilerWithPat())

    with pytest.raises(Exception) as excinfo:
        dc_settings.resolve_jira_datacenter_settings()
    assert "JIRA_PAT" in str(excinfo.value)
