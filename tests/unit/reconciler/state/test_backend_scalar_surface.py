"""The Backend facade supplies neutral project, query_project, and assert_env_ready
members. Create scope may default; read scope must not inherit that default.

Held-out oracles merged from test_backend_scalar_surface_heldout: Held-out scalar Backend-port
edges: read scope stays un-defaulted, readiness errors name the neutral BackendEnvError, and the
facade preserves structural compatibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg
from rebar.reconciler._backend import (
    Backend,
    BackendAssigneeNotFoundError,
    BackendEnvError,
)
from rebar.reconciler.adapters.jira.acli_subprocess import AssigneeNotFoundError
from rebar.reconciler.adapters.jira.backend import JiraBackend

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


def test_project_applies_create_default(tmp_path: Path, monkeypatch) -> None:
    """``project`` is UN-defaulted when nothing configures it — RP-04 S3 (AC2) removed
    the implicit create-time "DIG" default so an unset write scope stays "" and fails
    closed before any mutation, never silently targeting DIG."""
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    assert _backend().project == ""


def test_project_reads_configured_value(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[jira]\nproject = 'TEAM'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    cfg.reset_config_cache()
    assert _backend().project == "TEAM"


def test_query_project_reads_configured_value(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[jira]\nproject = 'TEAM'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    cfg.reset_config_cache()
    assert _backend().query_project == "TEAM"


def test_assert_env_ready_passes_when_all_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    monkeypatch.setenv("JIRA_URL", "https://j.example")
    monkeypatch.setenv("JIRA_USER", "u@x")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    cfg.reset_config_cache()
    assert _backend().assert_env_ready() is None


# ── held-out oracles (merged from test_backend_scalar_surface_heldout) ──


def _backend_heldout() -> JiraBackend:
    return JiraBackend(transport=object())


def test_query_project_undefaulted_when_unset(tmp_path: Path, monkeypatch) -> None:
    """The read scope is UN-defaulted: an unset project resolves to "" (fail-closed
    downstream in ``jql_active``), NOT to a create-time "DIG" default — bug 626d
    must never search all projects. RP-04 S3 (AC2) also un-defaulted the WRITE scope,
    so both now resolve to "" when unset."""
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    b = _backend_heldout()
    assert b.query_project == ""  # un-defaulted read scope
    assert b.project == ""


def test_assert_env_ready_raises_backend_env_error_naming_missing(
    tmp_path: Path, monkeypatch
) -> None:
    p = _proj(tmp_path)
    # url + user present via file, token absent → only JIRA_API_TOKEN missing.
    (p / "rebar.toml").write_text(
        "[jira]\nurl = 'https://j.example'\nuser = 'u@x'\n", encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    cfg.reset_config_cache()
    with pytest.raises(BackendEnvError, match="JIRA_API_TOKEN") as exc_info:
        _backend_heldout().assert_env_ready()
    msg = str(exc_info.value)
    assert "JIRA_URL" not in msg and "JIRA_USER" not in msg


def test_backend_env_error_is_runtimeerror() -> None:
    """``BackendEnvError`` subclasses ``RuntimeError`` so the pre-port
    ``except RuntimeError`` / ``pytest.raises(RuntimeError)`` contract still holds."""
    assert issubclass(BackendEnvError, RuntimeError)


def test_jira_assignee_error_subclasses_neutral_base() -> None:
    """The vendor assignee error IS-A neutral base (so core catches the base) AND
    still IS-A ValueError (so existing vendor-side raises/catches are unbroken)."""
    assert issubclass(AssigneeNotFoundError, BackendAssigneeNotFoundError)
    assert issubclass(AssigneeNotFoundError, ValueError)


def test_jira_backend_satisfies_widened_backend_protocol(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    assert isinstance(_backend_heldout(), Backend)
