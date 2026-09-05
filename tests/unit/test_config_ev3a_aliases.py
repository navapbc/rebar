"""EV-3a: core config-backed env names after alias retirement.

COMPACT_THRESHOLD and SCRATCH_BASE_DIR are retired by ADR 0116's clean-removal
policy: only REBAR_COMPACT_THRESHOLD and REBAR_SCRATCH_BASE_DIR are honored.
"""

from __future__ import annotations

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


def test_compact_legacy_alias_ignored_without_alias_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("COMPACT_THRESHOLD", "7")
    with caplog.at_level("WARNING", logger="rebar.config"):
        c = cfg.load_config(root=_proj(tmp_path))
    assert c.compact.threshold == 10
    assert not any("COMPACT_THRESHOLD" in r.getMessage() for r in caplog.records)


def test_compact_canonical_beats_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "5")
    monkeypatch.setenv("COMPACT_THRESHOLD", "99")
    assert cfg.load_config(root=_proj(tmp_path)).compact.threshold == 5


def test_compact_config_file(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[compact]\nthreshold = 13\n", encoding="utf-8")
    assert cfg.load_config(root=p).compact.threshold == 13


# ── scratch.base_dir ──────────────────────────────────────────────────────────
def test_scratch_canonical_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch_dir = tmp_path / "canon"
    monkeypatch.setenv("REBAR_SCRATCH_BASE_DIR", str(scratch_dir))
    assert cfg.load_config(root=_proj(tmp_path)).scratch.base_dir == str(scratch_dir)


def test_scratch_legacy_alias_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRATCH_BASE_DIR", str(tmp_path / "legacy"))
    assert cfg.load_config(root=_proj(tmp_path)).scratch.base_dir == ""


def test_scratch_base_dir_consumer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar._commands import scratch

    p = _proj(tmp_path)
    # default: <root>/.rebar/scratch
    assert scratch.base_dir(repo_root=str(p)).endswith("/.rebar/scratch")
    # canonical override
    monkeypatch.setenv("REBAR_SCRATCH_BASE_DIR", str(tmp_path / "sx"))
    cfg.reset_config_cache()
    assert scratch.base_dir(repo_root=str(p)) == str(tmp_path / "sx")
    monkeypatch.delenv("REBAR_SCRATCH_BASE_DIR")
    monkeypatch.setenv("SCRATCH_BASE_DIR", str(tmp_path / "sy"))
    cfg.reset_config_cache()
    assert scratch.base_dir(repo_root=str(p)).endswith("/.rebar/scratch")


# ── mcp.allow_jira_sync gate ──────────────────────────────────────────────────
def test_mcp_allow_jira_sync_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar import mcp_server

    monkeypatch.chdir(tmp_path)  # repo_root=None resolution
    monkeypatch.delenv("REBAR_ROOT", raising=False)  # cwd resolution under test
    assert mcp_server._allow_jira_sync() is False  # default off (fail-safe)
    monkeypatch.setenv("REBAR_MCP_ALLOW_JIRA_SYNC", "1")
    cfg.reset_config_cache()
    assert mcp_server._allow_jira_sync() is True
    # REBAR_MCP_ALLOW_RECONCILE_LIVE is a load-bearing TOMBSTONE (story 36c7): still
    # setting the removed sync-gate alias must FAIL LOUD (RemovedInputError, a
    # BaseException the gate's fail-safe ConfigError handler cannot swallow), not be
    # silently treated as "off".
    from rebar._deprecations import RemovedInputError

    monkeypatch.delenv("REBAR_MCP_ALLOW_JIRA_SYNC")
    monkeypatch.setenv("REBAR_MCP_ALLOW_RECONCILE_LIVE", "1")
    cfg.reset_config_cache()
    with pytest.raises(RemovedInputError, match="REBAR_MCP_ALLOW_JIRA_SYNC"):
        mcp_server._allow_jira_sync()


def test_mcp_allow_jira_sync_errors_on_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad boolean ERRORS the gate (operator ruling 39f8-ae7c) — never fails open,
    and never silently reads as the configured 'off' either."""
    from rebar import mcp_server
    from rebar.config import ConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REBAR_ROOT", raising=False)  # cwd resolution under test
    monkeypatch.setenv("REBAR_MCP_ALLOW_JIRA_SYNC", "maybe")  # invalid bool -> ConfigError
    cfg.reset_config_cache()
    with pytest.raises(ConfigError):
        mcp_server._allow_jira_sync()


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
    monkeypatch.delenv("REBAR_ROOT", raising=False)  # cwd resolution under test
    assert mcp_server._readonly() is False  # default
    (p / "rebar.toml").write_text("[mcp]\nreadonly = true\n", encoding="utf-8")
    cfg.reset_config_cache()
    assert mcp_server._readonly() is True  # config-file honored
    monkeypatch.setenv("REBAR_MCP_READONLY", "0")  # env overrides the file
    cfg.reset_config_cache()
    assert mcp_server._readonly() is False


def test_mcp_readonly_errors_on_malformed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed config ERRORS the read-only gate (operator ruling 39f8-ae7c) — the
    fault surfaces to the operator instead of silently reading as configured
    read-only (the pre-ruling posture failed closed, which withheld writes but
    laundered the fault into a policy value)."""
    from rebar import mcp_server
    from rebar.config import ConfigError

    p = _proj_git(tmp_path)
    monkeypatch.chdir(p)
    monkeypatch.delenv("REBAR_ROOT", raising=False)  # cwd resolution under test
    (p / "pyproject.toml").write_text("[tool.rebar] broken === [[\n", encoding="utf-8")
    cfg.reset_config_cache()
    with pytest.raises(ConfigError):
        mcp_server._readonly()


def test_mcp_allow_llm_gate_and_error_on_bad_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar import mcp_server
    from rebar.config import ConfigError

    p = _proj_git(tmp_path)
    monkeypatch.chdir(p)
    monkeypatch.delenv("REBAR_ROOT", raising=False)  # cwd resolution under test
    assert mcp_server._allow_llm() is False  # default off
    (p / "rebar.toml").write_text("[mcp]\nallow_llm = true\n", encoding="utf-8")
    cfg.reset_config_cache()
    assert mcp_server._allow_llm() is True  # config-file honored
    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "garbage")  # invalid -> ConfigError
    cfg.reset_config_cache()
    with pytest.raises(ConfigError):  # errors per 39f8-ae7c — never a silent default
        mcp_server._allow_llm()
