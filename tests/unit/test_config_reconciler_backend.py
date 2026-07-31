"""Config-load validation of reconciler.backend (S3, epic bbf1).

The backend selector is a first-class typed field on ``ReconcilerConfig``
(``load_config().reconciler.backend``), mirroring ``jira_cli_timeout``. Its coercer
is the ``_as_choice`` static-key validator (same mechanism as ``sync.push`` /
``mcp.transport``), so an unknown key is rejected at CONFIG-LOAD time (not deferred to
the registry) with a ``ConfigError`` naming the allowed choices, and the
``REBAR_RECONCILER_BACKEND`` env override is auto-derived.
"""

from __future__ import annotations

import textwrap

import pytest

from rebar.config import ConfigError, load_config


def _write_project_config(tmp_path, body: str):
    (tmp_path / "rebar.toml").write_text(textwrap.dedent(body))


def test_default_backend_is_jira(tmp_path, monkeypatch):
    monkeypatch.delenv("REBAR_RECONCILER_BACKEND", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_project_config(tmp_path, "[reconciler]\n")
    assert load_config(root=tmp_path).reconciler.backend == "jira"


def test_explicit_jira_backend_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("REBAR_RECONCILER_BACKEND", raising=False)
    _write_project_config(
        tmp_path,
        """
        [reconciler]
        backend = "jira"
        """,
    )
    assert load_config(root=tmp_path).reconciler.backend == "jira"


def test_unknown_backend_rejected_at_config_load(tmp_path, monkeypatch):
    # HELD-OUT edge: a bogus key fails at load with a ConfigError naming choices.
    monkeypatch.delenv("REBAR_RECONCILER_BACKEND", raising=False)
    _write_project_config(
        tmp_path,
        """
        [reconciler]
        backend = "github"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(root=tmp_path)
    assert "reconciler.backend" in str(exc.value)
    assert "jira" in str(exc.value)


def test_env_override_resolves(tmp_path, monkeypatch):
    # HELD-OUT edge: REBAR_RECONCILER_BACKEND is auto-derived and honoured.
    _write_project_config(tmp_path, '[reconciler]\nbackend = "jira"\n')
    monkeypatch.setenv("REBAR_RECONCILER_BACKEND", "jira")
    assert load_config(root=tmp_path).reconciler.backend == "jira"


def test_jira_datacenter_backend_loads(tmp_path, monkeypatch):
    """Story J7 (epic e369): the DC backend was REGISTERED but UNREACHABLE — the
    registry resolved ``jira-datacenter`` while this validator still rejected it,
    so no config could ever select it."""
    monkeypatch.delenv("REBAR_RECONCILER_BACKEND", raising=False)
    _write_project_config(
        tmp_path,
        """
        [reconciler]
        backend = "jira-datacenter"
        """,
    )
    assert load_config(root=tmp_path).reconciler.backend == "jira-datacenter"


def test_env_override_resolves_jira_datacenter(tmp_path, monkeypatch):
    # HELD-OUT edge: the auto-derived env override reaches the NEW choice too, not
    # just the pre-existing one.
    _write_project_config(tmp_path, "[reconciler]\n")
    monkeypatch.setenv("REBAR_RECONCILER_BACKEND", "jira-datacenter")
    assert load_config(root=tmp_path).reconciler.backend == "jira-datacenter"


def test_jira_server_still_rejected(tmp_path, monkeypatch):
    """HELD-OUT edge: widening the choice set must not turn it into a free-for-all.
    ``jira-server`` is the plausible-looking near-miss for a Data Center deployment
    (Server and Data Center are the same product line), so it is the one an operator
    is most likely to type — it must still fail at load, naming the real choices."""
    monkeypatch.delenv("REBAR_RECONCILER_BACKEND", raising=False)
    _write_project_config(
        tmp_path,
        """
        [reconciler]
        backend = "jira-server"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(root=tmp_path)
    message = str(exc.value)
    assert "reconciler.backend" in message
    assert "jira-datacenter" in message, (
        f"the error must list the valid choices, including the new one: {message}"
    )


def test_env_override_unknown_rejected(tmp_path, monkeypatch):
    # HELD-OUT edge: an unknown env override is rejected at load, same as file.
    _write_project_config(tmp_path, "[reconciler]\n")
    monkeypatch.setenv("REBAR_RECONCILER_BACKEND", "nope")
    with pytest.raises(ConfigError) as exc:
        load_config(root=tmp_path)
    assert "jira" in str(exc.value)
