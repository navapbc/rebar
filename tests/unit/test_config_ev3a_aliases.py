"""EV-3a: core config-backed env renames with deprecation aliases —
COMPACT_THRESHOLD->REBAR_COMPACT_THRESHOLD (compact.threshold),
SCRATCH_BASE_DIR->REBAR_SCRATCH_BASE_DIR (scratch.base_dir),
REBAR_MCP_ALLOW_RECONCILE_LIVE->REBAR_MCP_ALLOW_JIRA_SYNC (mcp.allow_jira_sync).
Verifies canonical names, legacy aliases (warn + map), canonical-wins precedence,
and the consumers (scratch.base_dir, the MCP jira-sync gate fail-safe).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rebar import config as cfg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_CONFIG",
        "XDG_CONFIG_HOME",
        "REBAR_COMPACT_THRESHOLD",
        "COMPACT_THRESHOLD",
        "REBAR_SCRATCH_BASE_DIR",
        "SCRATCH_BASE_DIR",
        "REBAR_MCP_ALLOW_JIRA_SYNC",
        "REBAR_MCP_ALLOW_RECONCILE_LIVE",
        "REBAR_MCP_READONLY",
        "REBAR_MCP_ALLOW_LLM",
    ):
        monkeypatch.delenv(name, raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


# ── compact.threshold ─────────────────────────────────────────────────────────
def test_compact_canonical_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "42")
    assert cfg.load_config(root=_proj(tmp_path)).compact.threshold == 42


def test_compact_legacy_alias_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setenv("COMPACT_THRESHOLD", "7")
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=_proj(tmp_path))
    assert c.compact.threshold == 7
    assert any(
        "COMPACT_THRESHOLD" in r.getMessage() and "deprecated" in r.getMessage()
        for r in caplog.records
    )


def test_compact_canonical_beats_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "5")
    monkeypatch.setenv("COMPACT_THRESHOLD", "99")
    assert cfg.load_config(root=_proj(tmp_path)).compact.threshold == 5


def test_compact_config_file(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[compact]\nthreshold = 13\n", encoding="utf-8")
    assert cfg.load_config(root=p).compact.threshold == 13


# ── scratch.base_dir ──────────────────────────────────────────────────────────
def test_scratch_canonical_and_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_SCRATCH_BASE_DIR", "/tmp/canon")
    assert cfg.load_config(root=_proj(tmp_path)).scratch.base_dir == "/tmp/canon"
    monkeypatch.delenv("REBAR_SCRATCH_BASE_DIR")
    monkeypatch.setenv("SCRATCH_BASE_DIR", "/tmp/legacy")
    cfg.reset_config_cache()
    assert cfg.load_config(root=_proj(tmp_path / "b")).scratch.base_dir == "/tmp/legacy"


def test_scratch_base_dir_consumer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar._commands import scratch

    p = _proj(tmp_path)
    # default: <root>/.rebar/scratch
    assert scratch.base_dir(repo_root=str(p)).endswith("/.rebar/scratch")
    # canonical override
    monkeypatch.setenv("REBAR_SCRATCH_BASE_DIR", str(tmp_path / "sx"))
    cfg.reset_config_cache()
    assert scratch.base_dir(repo_root=str(p)) == str(tmp_path / "sx")
    # legacy alias
    monkeypatch.delenv("REBAR_SCRATCH_BASE_DIR")
    monkeypatch.setenv("SCRATCH_BASE_DIR", str(tmp_path / "sy"))
    cfg.reset_config_cache()
    assert scratch.base_dir(repo_root=str(p)) == str(tmp_path / "sy")


# ── mcp.allow_jira_sync gate ──────────────────────────────────────────────────
def test_mcp_allow_jira_sync_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar import mcp_server

    monkeypatch.chdir(tmp_path)  # repo_root=None resolution
    assert mcp_server._allow_jira_sync() is False  # default off (fail-safe)
    monkeypatch.setenv("REBAR_MCP_ALLOW_JIRA_SYNC", "1")
    cfg.reset_config_cache()
    assert mcp_server._allow_jira_sync() is True
    # legacy alias still enables it
    monkeypatch.delenv("REBAR_MCP_ALLOW_JIRA_SYNC")
    monkeypatch.setenv("REBAR_MCP_ALLOW_RECONCILE_LIVE", "1")
    cfg.reset_config_cache()
    assert mcp_server._allow_jira_sync() is True


def test_mcp_allow_jira_sync_failsafe_on_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad boolean must resolve the gate CLOSED (off), never crash or fail open."""
    from rebar import mcp_server

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REBAR_MCP_ALLOW_JIRA_SYNC", "maybe")  # invalid bool -> ConfigError
    cfg.reset_config_cache()
    assert mcp_server._allow_jira_sync() is False  # fail-safe off


# ── mcp.readonly / mcp.allow_llm gates: reported == enforced (review fix) ──────
def _proj_git(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


def test_mcp_readonly_honors_config_file_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readonly gate now reads the typed config, so a [tool.rebar.mcp] config
    file is honored (was previously env-only while `rebar config` reported it) — and
    env still wins."""
    from rebar import mcp_server

    p = _proj_git(tmp_path)
    monkeypatch.chdir(p)
    assert mcp_server._readonly() is False  # default
    (p / "rebar.toml").write_text("[mcp]\nreadonly = true\n", encoding="utf-8")
    cfg.reset_config_cache()
    assert mcp_server._readonly() is True  # config-file honored
    monkeypatch.setenv("REBAR_MCP_READONLY", "0")  # env overrides the file
    cfg.reset_config_cache()
    assert mcp_server._readonly() is False


def test_mcp_readonly_fails_closed_on_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed config locks the server READ-ONLY (fail-closed, like the verify
    gate) — never exposes write tools on a broken config."""
    from rebar import mcp_server

    p = _proj_git(tmp_path)
    monkeypatch.chdir(p)
    (p / "pyproject.toml").write_text("[tool.rebar] broken === [[\n", encoding="utf-8")
    cfg.reset_config_cache()
    assert mcp_server._readonly() is True  # fail-CLOSED


def test_mcp_allow_llm_gate_and_failsafe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar import mcp_server

    p = _proj_git(tmp_path)
    monkeypatch.chdir(p)
    assert mcp_server._allow_llm() is False  # default off
    (p / "rebar.toml").write_text("[mcp]\nallow_llm = true\n", encoding="utf-8")
    cfg.reset_config_cache()
    assert mcp_server._allow_llm() is True  # config-file honored
    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "garbage")  # invalid -> ConfigError
    cfg.reset_config_cache()
    assert mcp_server._allow_llm() is False  # fail-safe off (never enable on bad config)
