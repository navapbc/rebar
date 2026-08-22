"""An unreadable config ERRORS the MCP gate resolvers — never a silent default (8408).

Operator ruling 39f8-ae7c ("Unreadable config should result in an error"), extended to
the MCP surface: ``config.mcp_readonly()`` and ``config.mcp_gate()`` must raise a typed
:class:`~rebar.config.ConfigError` (chained from the parse fault, naming the gate and
the ruling) instead of collapsing the fault into the configured/default value — even a
SAFE default, because a fault that reads as a policy choice never gets fixed. The MCP
error envelope classifies the fault as ``config_unreadable`` so a driving agent can
branch on it (change 2045 PS4 surface-parity advisory).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import config as cfg
from rebar.config import ConfigError

# `[tool.rebar` never closes its table header -> tomllib raises -> ConfigError.
_BROKEN = "[tool.rebar\nbroken = true\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate from the host env — a set ``REBAR_MCP_*`` (env wins over the file)
    or config-path override would change what the resolvers see (CI sets
    ``REBAR_MCP_ALLOW_LLM``, which broke this module's default-value assertions)."""
    for name in (
        "REBAR_CONFIG",
        "XDG_CONFIG_HOME",
        "REBAR_MCP_ALLOW_JIRA_SYNC",
        "REBAR_MCP_READONLY",
        "REBAR_MCP_ALLOW_LLM",
    ):
        monkeypatch.delenv(name, raising=False)


def _proj_git(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


@pytest.fixture
def broken_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = _proj_git(tmp_path)
    monkeypatch.chdir(p)
    monkeypatch.delenv("REBAR_ROOT", raising=False)  # cwd resolution under test
    (p / "pyproject.toml").write_text(_BROKEN, encoding="utf-8")
    cfg.reset_config_cache()
    return p


# ── the resolvers raise, typed and chained ─────────────────────────────────────


def test_mcp_readonly_raises_on_malformed_config(broken_config: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        cfg.mcp_readonly()
    assert "39f8-ae7c" in str(excinfo.value), (
        f"the error does not cite the operator ruling: {str(excinfo.value)!r}"
    )
    assert "read-only" in str(excinfo.value), (
        f"the error does not name the gate: {str(excinfo.value)!r}"
    )
    assert isinstance(excinfo.value.__cause__, ConfigError), (
        "the raise must be chained `from` the underlying parse fault"
    )


def test_mcp_gate_raises_on_malformed_config(broken_config: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        cfg.mcp_gate("allow_llm")
    assert "39f8-ae7c" in str(excinfo.value)
    assert "allow_llm" in str(excinfo.value), (
        f"the error does not name the gate attr: {str(excinfo.value)!r}"
    )
    assert isinstance(excinfo.value.__cause__, ConfigError)


def test_mcp_gate_fail_fallback_is_gone() -> None:
    """The `fail` kwarg existed only to pick the malformed-config fallback; with the
    error posture it is dead and removed — no caller can silently choose a default."""
    import inspect

    assert "fail" not in inspect.signature(cfg.mcp_gate).parameters


# ── consumers propagate (no swallow anywhere on the path) ──────────────────────


def test_mcp_server_consumers_propagate(broken_config: Path) -> None:
    from rebar import mcp_server

    with pytest.raises(ConfigError):
        mcp_server._readonly()
    with pytest.raises(ConfigError):
        mcp_server._allow_llm()
    with pytest.raises(ConfigError):
        mcp_server._allow_jira_sync()


def test_runner_readonly_gate_propagates(broken_config: Path) -> None:
    from rebar.llm import runner_support

    with pytest.raises(ConfigError):
        runner_support._readonly_gate()


# ── readable-config behaviour is byte-identical ────────────────────────────────


def test_readable_config_resolution_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _proj_git(tmp_path)
    monkeypatch.chdir(p)
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    cfg.reset_config_cache()
    assert cfg.mcp_readonly() is False  # default
    assert cfg.mcp_gate("allow_llm") is False  # default off
    (p / "rebar.toml").write_text("[mcp]\nreadonly = true\nallow_llm = true\n", encoding="utf-8")
    cfg.reset_config_cache()
    assert cfg.mcp_readonly() is True  # file key honored
    assert cfg.mcp_gate("allow_llm") is True
    monkeypatch.setenv("REBAR_MCP_READONLY", "0")  # env wins over the file
    cfg.reset_config_cache()
    assert cfg.mcp_readonly() is False


# ── the MCP envelope classifies the fault ──────────────────────────────────────


def test_error_code_for_classifies_config_unreadable() -> None:
    assert rebar.error_code_for(ConfigError("boom")) == "config_unreadable"
    assert "config_unreadable" in rebar.KNOWN_ERROR_CODES


def test_envelope_guard_knows_config_error() -> None:
    from rebar._mcp_errors import McpEnvelopeError, _envelope_error

    err = _envelope_error(ConfigError("the config could not be read"))
    assert isinstance(err, McpEnvelopeError), (
        "an unreadable-config fault must reach MCP consumers as the structured envelope"
    )
    assert err.envelope["error"] == "config_unreadable"
