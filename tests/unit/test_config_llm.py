"""0ac6 (slice 4): [tool.rebar.llm] config-FILE support in LLMConfig.from_env.

llm.* is resolved by the optional rebar.llm layer (NOT the stdlib-core typed Config,
so importing rebar.llm never pulls the agents stack). The non-secret knobs are
settable in a [tool.rebar.llm] table (pyproject / rebar.toml [llm] / legacy
.rebar/config.conf llm.* / XDG user) read via the core loader's discovery, with
resolution CLI (`rebar -c llm.KEY=VALUE`) > REBAR_LLM_<KEY> env > file > default.
Secrets (api_key) + the runtime-only repo_path + the DERIVED runner stay env-only.

Also pins the cross-cutting invariant: a [tool.rebar.llm] table must NOT make the
stdlib-core load_config warn (llm is a reserved section) nor appear on Config.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rebar import config as cfg
from rebar.llm.config import DEFAULT_MODEL, LLMConfig

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_CONFIG",
        "XDG_CONFIG_HOME",
        "REBAR_ROOT",
        "REBAR_LLM_MODEL",
        "REBAR_LLM_MODEL_PROVIDER",
        "REBAR_LLM_BASE_URL",
        "REBAR_LLM_API_KEY",
        "REBAR_LLM_MAX_TOKENS",
        "REBAR_LLM_MAX_STEPS",
        "REBAR_LLM_MAX_ITERS",
        "REBAR_LLM_TIMEOUT",
        "REBAR_LLM_MCP_SERVERS",
        "REBAR_LLM_REPO_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg.set_cli_overrides(None)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


def _xdg(tmp: Path, body: str) -> Path:
    base = tmp / "xdg"
    (base / "rebar").mkdir(parents=True)
    (base / "rebar" / "config.toml").write_text(body, encoding="utf-8")
    return base


def _cfg(root: Path) -> LLMConfig:
    cfg.reset_config_cache()
    return LLMConfig.from_env(repo_root=root)


# ── defaults ──────────────────────────────────────────────────────────────────
def test_defaults(tmp_path: Path) -> None:
    o = _cfg(_proj(tmp_path))
    assert o.model == DEFAULT_MODEL
    assert (o.max_tokens, o.max_iterations, o.timeout_s) == (8000, 25, 600)
    assert o.model_provider is None and o.base_url is None and o.mcp_servers == {}


# ── config-file locations are consumed ────────────────────────────────────────
def test_pyproject(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text(
        "[tool.rebar.llm]\nmodel = 'claude-sonnet-4-6'\nmax_tokens = 4000\n"
        "max_steps = 10\ntimeout = 300\nmodel_provider = 'anthropic'\n"
        "base_url = 'https://local'\nmcp_servers = { fs = { command = 'x' } }\n",
        encoding="utf-8",
    )
    o = _cfg(p)
    assert o.model == "claude-sonnet-4-6"
    assert (o.max_tokens, o.max_iterations, o.timeout_s) == (4000, 10, 300)
    assert o.model_provider == "anthropic" and o.base_url == "https://local"
    assert o.mcp_servers == {"fs": {"command": "x"}}


def test_rebar_toml_top_level_llm(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm]\nmodel = 'claude-haiku-4-5'\n", encoding="utf-8")
    assert _cfg(p).model == "claude-haiku-4-5"


def test_legacy_dotted_conf(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / ".rebar").mkdir()
    (p / ".rebar" / "config.conf").write_text(
        "llm.model=claude-opus-4-8\nllm.max_steps=7\n", encoding="utf-8"
    )
    o = _cfg(p)
    assert o.model == "claude-opus-4-8" and o.max_iterations == 7


def test_xdg_user_config(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    base = _xdg(tmp_path, "[llm]\nmodel = 'user-model'\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base))
    assert _cfg(p).model == "user-model"


# ── precedence CLI > env > project > user > default ───────────────────────────
def test_precedence_full_chain(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    base = _xdg(tmp_path, "[llm]\nmodel = 'user-model'\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base))
    assert _cfg(p).model == "user-model"  # user only
    (p / "rebar.toml").write_text("[llm]\nmodel = 'project-model'\n", encoding="utf-8")
    assert _cfg(p).model == "project-model"  # project beats user
    monkeypatch.setenv("REBAR_LLM_MODEL", "env-model")
    assert _cfg(p).model == "env-model"  # env beats project
    cfg.set_cli_overrides(cfg.parse_cli_overrides(["llm.model=cli-model"]))
    assert LLMConfig.from_env(repo_root=p).model == "cli-model"  # cli beats env
    cfg.set_cli_overrides(None)


def test_max_steps_legacy_env_alias_beats_file(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm]\nmax_steps = 5\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_LLM_MAX_ITERS", "40")  # deprecated alias, still env layer
    assert _cfg(p).max_iterations == 40


# ── mcp_servers: env JSON overrides a file inline table; bad JSON → {} ────────
def test_mcp_servers_env_json_overrides_file(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm.mcp_servers.fs]\ncommand = 'from-file'\n", encoding="utf-8")
    assert _cfg(p).mcp_servers == {"fs": {"command": "from-file"}}  # file consumed
    monkeypatch.setenv("REBAR_LLM_MCP_SERVERS", '{"net": {"command": "from-env"}}')
    assert _cfg(p).mcp_servers == {"net": {"command": "from-env"}}  # env JSON wins
    monkeypatch.setenv("REBAR_LLM_MCP_SERVERS", "{not valid json")
    assert _cfg(p).mcp_servers == {}  # malformed env JSON → empty (prior behavior)


# ── secret stays env-only ─────────────────────────────────────────────────────
def test_api_key_env_only(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm]\napi_key = 'leaked-via-file'\n", encoding="utf-8")
    assert _cfg(p).api_key is None  # a config file cannot set the secret
    monkeypatch.setenv("REBAR_LLM_API_KEY", "from-env")
    assert _cfg(p).api_key == "from-env"


def test_repo_path_is_runtime_env_only(tmp_path: Path, monkeypatch) -> None:
    """repo_path is an invocation-specific runtime override — env only, not a file key."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm]\nrepo_path = '/file/path'\n", encoding="utf-8")
    assert _cfg(p).repo_path != "/file/path"  # file key ignored
    monkeypatch.setenv("REBAR_LLM_REPO_PATH", "/env/path")
    assert _cfg(p).repo_path == "/env/path"


# ── malformed config degrades to env-only (never breaks an LLM op) ────────────
def test_malformed_config_falls_back_to_env(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm]\nmodel = [ broken toml\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_LLM_MODEL", "env-model")
    assert _cfg(p).model == "env-model"


# ── cross-cutting: the core loader treats [tool.rebar.llm] as a reserved section ─
def test_core_load_config_does_not_warn_on_llm_table(tmp_path: Path, caplog) -> None:
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text(
        "[tool.rebar.llm]\nmodel = 'x'\n[tool.rebar.sync]\npush = 'off'\n", encoding="utf-8"
    )
    cfg.reset_config_cache()
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        conf = cfg.load_config(root=p)
    assert conf.sync.push == "off"  # a real core key alongside [tool.rebar.llm] still resolves
    assert not hasattr(conf, "llm")  # llm is NOT a core Config field (stdlib boundary)
    assert not any("llm" in r.getMessage().lower() for r in caplog.records)


def test_llm_table_absent_from_show_config_sources(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm]\nmodel = 'x'\n", encoding="utf-8")
    cfg.reset_config_cache()
    _, sources, _ = cfg.resolve_with_sources(root=p)
    assert "llm" not in sources  # reserved section is not part of core provenance
