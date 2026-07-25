"""Config-load validation of ``reconciler.backend = jira-datacenter``.

The Data Center key widens the ``_as_choice`` choice-set alongside ``jira``, so it
loads from the file, resolves through the auto-derived ``REBAR_RECONCILER_BACKEND``
env override, and an unknown key is still rejected at load with a ``ConfigError``.
"""

from __future__ import annotations

import textwrap

import pytest

from rebar.config import ConfigError, load_config


def _write_project_config(tmp_path, body: str):
    (tmp_path / "rebar.toml").write_text(textwrap.dedent(body))


def test_jira_datacenter_backend_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("REBAR_RECONCILER_BACKEND", raising=False)
    _write_project_config(
        tmp_path,
        """
        [reconciler]
        backend = "jira-datacenter"
        """,
    )
    assert load_config(root=tmp_path).reconciler.backend == "jira-datacenter"


def test_env_override_to_jira_datacenter(tmp_path, monkeypatch):
    _write_project_config(tmp_path, "[reconciler]\n")
    monkeypatch.setenv("REBAR_RECONCILER_BACKEND", "jira-datacenter")
    assert load_config(root=tmp_path).reconciler.backend == "jira-datacenter"


def test_unknown_backend_still_rejected(tmp_path, monkeypatch):
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
    assert "reconciler.backend" in str(exc.value)
    assert "jira-datacenter" in str(exc.value)
