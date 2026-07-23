"""Ticket 97f2 (HELD-OUT edge oracle): the scalar Backend-port surface.

Withheld from the implementer: the edge/boundary contracts that separate a real port
implementation from one that only satisfies the happy path — the un-defaulted
fail-closed read scope, the exact BackendEnvError naming, the RuntimeError lineage,
the neutral-base subclass relationship, and the widened-facade structural check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg
from rebar_reconciler._backend import (
    Backend,
    BackendAssigneeNotFoundError,
    BackendEnvError,
)
from rebar_reconciler.adapters.jira.acli_subprocess import AssigneeNotFoundError
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
    return JiraBackend(transport=object())


# ── query_project is UN-defaulted (fail-closed), unlike project ──────────────
def test_query_project_undefaulted_when_unset(tmp_path: Path, monkeypatch) -> None:
    """The read scope is UN-defaulted: an unset project resolves to "" (fail-closed
    downstream in ``jql_active``), NOT to the create-time "DIG" default — bug 626d
    must never search all projects. This is the contrast vs ``project``."""
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    b = _backend()
    assert b.query_project == ""  # un-defaulted read scope
    assert b.project == "DIG"  # write scope still defaulted — the two differ


# ── assert_env_ready raises the neutral error, naming exactly what's missing ─
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
        _backend().assert_env_ready()
    msg = str(exc_info.value)
    assert "JIRA_URL" not in msg and "JIRA_USER" not in msg  # only the missing one named


def test_backend_env_error_is_runtimeerror() -> None:
    """``BackendEnvError`` subclasses ``RuntimeError`` so the pre-port
    ``except RuntimeError`` / ``pytest.raises(RuntimeError)`` contract still holds."""
    assert issubclass(BackendEnvError, RuntimeError)


# ── BackendAssigneeNotFoundError (neutral base) ──────────────────────────────
def test_jira_assignee_error_subclasses_neutral_base() -> None:
    """The vendor assignee error IS-A neutral base (so core catches the base) AND
    still IS-A ValueError (so existing vendor-side raises/catches are unbroken)."""
    assert issubclass(AssigneeNotFoundError, BackendAssigneeNotFoundError)
    assert issubclass(AssigneeNotFoundError, ValueError)


# ── the widened facade is structurally satisfied ────────────────────────────
def test_jira_backend_satisfies_widened_backend_protocol(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    assert isinstance(_backend(), Backend)
