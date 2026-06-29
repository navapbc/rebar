"""ticket.default_assignee config key (story c36c): a ticket-workflow default applied
by `claim` when no assignee is given. Read from the config file ([ticket] section) and
overridable by the canonical env var REBAR_DEFAULT_ASSIGNEE (env > file > default).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    monkeypatch.delenv("REBAR_TICKET_DEFAULT_ASSIGNEE", raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


def test_default_is_empty(tmp_path: Path) -> None:
    assert cfg.load_config(root=_proj(tmp_path)).ticket.default_assignee == ""


def test_from_toml(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        '[ticket]\ndefault_assignee = "dana@example.com"\n', encoding="utf-8"
    )
    assert cfg.load_config(root=p).ticket.default_assignee == "dana@example.com"


def test_from_pyproject(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text(
        '[tool.rebar.ticket]\ndefault_assignee = "x@example.com"\n', encoding="utf-8"
    )
    assert cfg.load_config(root=p).ticket.default_assignee == "x@example.com"


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        '[ticket]\ndefault_assignee = "file@example.com"\n', encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_DEFAULT_ASSIGNEE", "env@example.com")
    assert cfg.load_config(root=p).ticket.default_assignee == "env@example.com"


def test_canonical_env_name_is_rebar_default_assignee() -> None:
    """The canonical env override is the ergonomic REBAR_DEFAULT_ASSIGNEE, not the
    auto-derived REBAR_TICKET_DEFAULT_ASSIGNEE."""
    assert cfg._canonical_env_name("ticket", "default_assignee") == "REBAR_DEFAULT_ASSIGNEE"


def test_key_is_known_no_unknown_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Setting [ticket] default_assignee must not emit an 'unknown key' warning."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        '[ticket]\ndefault_assignee = "ok@example.com"\n', encoding="utf-8"
    )
    with caplog.at_level("WARNING"):
        cfg.load_config(root=p)
    assert not any("default_assignee" in r.message for r in caplog.records)
