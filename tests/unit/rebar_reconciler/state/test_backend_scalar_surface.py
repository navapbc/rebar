"""Ticket 97f2: the scalar Backend-port surface (epic bbf1, ADR 0035 §(d)).

The Backend facade gains vendor-neutral scalar members so the reconciler core no
longer reaches ``adapters.jira`` for project scope / env-readiness:

* ``project`` — the write/create scope with the create-time default applied
  (Jira: ``resolve_jira_settings(project_default="DIG").project``).
* ``query_project`` — the read/query scope WITHOUT any create-time default.
* ``assert_env_ready()`` — raises the neutral ``BackendEnvError`` when a connection
  essential is missing (Jira: JIRA_URL/JIRA_USER/JIRA_API_TOKEN).

Happy-path oracle: the members exist and return/pass on well-formed configuration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg
from rebar_reconciler.adapters.jira.backend import JiraBackend

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_CONFIG",
        "XDG_CONFIG_HOME",
        "REBAR_ROOT",
        "JIRA_URL",
        "JIRA_USER",
        "JIRA_PROJECT",
        "JIRA_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg.reset_config_cache()


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


def _backend() -> JiraBackend:
    """A JiraBackend whose scalar accessors resolve from config/env (the transport
    is irrelevant to these members — they read ``resolve_jira_settings``)."""
    return JiraBackend(transport=object())


# ── project (write/create scope, DIG-defaulted) ──────────────────────────────
def test_project_applies_create_default(tmp_path: Path, monkeypatch) -> None:
    """``project`` returns the create-time default ("DIG") when nothing configures it,
    matching the applier's cross-project guard scope."""
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    assert _backend().project == "DIG"


def test_project_reads_configured_value(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[jira]\nproject = 'TEAM'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    cfg.reset_config_cache()
    assert _backend().project == "TEAM"


# ── query_project (read/query scope) ─────────────────────────────────────────
def test_query_project_reads_configured_value(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[jira]\nproject = 'TEAM'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    cfg.reset_config_cache()
    assert _backend().query_project == "TEAM"


# ── assert_env_ready (passes when all essentials present) ────────────────────
def test_assert_env_ready_passes_when_all_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    monkeypatch.setenv("JIRA_URL", "https://j.example")
    monkeypatch.setenv("JIRA_USER", "u@x")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    cfg.reset_config_cache()
    assert _backend().assert_env_ready() is None  # no raise
