"""0ac6 (slice 4): [tool.rebar.llm] config-FILE support in LLMConfig.from_env.

llm.* is resolved by the optional rebar.llm layer (NOT the stdlib-core typed Config,
so importing rebar.llm never pulls the agents stack). The non-secret knobs are
settable in a [tool.rebar.llm] table (pyproject / rebar.toml [llm] / XDG user) read
via the core loader's discovery, with
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
        "REBAR_LLM_RETRY_MAX_ATTEMPTS",
        "REBAR_LLM_RETRY_MAX_WAIT_S",
        "REBAR_LLM_TOOL_TIMEOUT_S",
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
    assert (o.max_tokens, o.max_iterations, o.timeout_s) == (16000, 250, 600)
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


def test_rebar_toml_llm_table(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[llm]\nmodel = 'claude-opus-4-8'\nmax_steps = 7\n", encoding="utf-8"
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


def test_max_iters_env_is_a_load_bearing_tombstone(tmp_path: Path, monkeypatch) -> None:
    # REBAR_LLM_MAX_ITERS is a load-bearing TOMBSTONE (story 36c7): still-setting the
    # removed knob must FAIL LOUD from LLMConfig.from_env with a RemovedInputError naming
    # REBAR_LLM_MAX_STEPS — not be silently ignored (a silent drop reverts the operator's
    # intended step budget to the default).
    from rebar._deprecations import RemovedInputError

    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm]\nmax_steps = 5\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_LLM_MAX_ITERS", "40")
    with pytest.raises(RemovedInputError, match="REBAR_LLM_MAX_STEPS"):
        _cfg(p)
    # The canonical name is honored (no tombstone), overriding the file value.
    monkeypatch.delenv("REBAR_LLM_MAX_ITERS")
    monkeypatch.setenv("REBAR_LLM_MAX_STEPS", "40")
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


# ── the single code-read-root resolver (epic 5ca8 / no_repo_root class) ────────
def test_resolve_code_root_cascade(tmp_path: Path) -> None:
    """The authoritative code-read-root resolver: explicit > cfg.repo_path > active gate
    snapshot > live checkout, and NEVER None — the contract that kills the no_repo_root
    class of bug (a gate consumer handed None silently degrading)."""
    from rebar.llm import config as llm_config

    # 1. An explicit repo_root wins over everything.
    assert llm_config.resolve_code_root("/explicit", cfg_repo_path="/snap") == "/explicit"
    # 2. A pinned cfg.repo_path is next.
    assert llm_config.resolve_code_root(None, cfg_repo_path="/snap") == "/snap"
    # 3. The ACTIVE attested-gate snapshot (origin/main HEAD by default) is next.
    token = llm_config._active_code_root.set(str(tmp_path))
    try:
        assert llm_config.resolve_code_root() == str(tmp_path)
        assert llm_config.resolve_code_root(cfg_repo_path="/snap") == "/snap"  # still below cfg
    finally:
        llm_config._active_code_root.reset(token)
    # 4. Nothing specified, no gate active → the live checkout, a real path string, NEVER None.
    fallback = llm_config.resolve_code_root()
    assert isinstance(fallback, str) and fallback
    # 5. With the checkout fallback OPTED OUT (the lightweight-builder mode), the same
    #    no-context call returns None instead of forcing a checkout root.
    assert llm_config.resolve_code_root(allow_checkout_fallback=False) is None


def test_resolve_code_root_require_raises_on_unresolved(tmp_path: Path) -> None:
    """The read-root CONTRACT (epic drag-gripe-brake): a stage that REQUIRES a root must not run
    blind against None. ``require=True`` RAISES (fail-closed) when the cascade would yield None —
    asserted by exception TYPE (no string heuristics) — while a resolvable root (an active
    snapshot, or an explicit root) satisfies it without raising, and ``require=False`` preserves
    the snapshot-or-None behavior."""
    from rebar.llm import config as llm_config
    from rebar.llm.errors import LLMConfigError

    # Unresolved + require=True (only reachable with the checkout fallback opted out) → loud.
    with pytest.raises(LLMConfigError):
        llm_config.resolve_code_root(allow_checkout_fallback=False, require=True)
    # Unresolved + require=False → the prior snapshot-or-None behavior (no raise).
    assert llm_config.resolve_code_root(allow_checkout_fallback=False, require=False) is None
    # An ACTIVE snapshot satisfies require=True without raising (the #71 cascade grounds it).
    token = llm_config._active_code_root.set(str(tmp_path))
    try:
        assert llm_config.resolve_code_root(allow_checkout_fallback=False, require=True) == str(
            tmp_path
        )
    finally:
        llm_config._active_code_root.reset(token)
    # An explicit root also satisfies it (no raise even with the fallback opted out).
    assert (
        llm_config.resolve_code_root("/explicit", allow_checkout_fallback=False, require=True)
        == "/explicit"
    )


def test_resolve_gate_config_prefers_active_then_env(tmp_path: Path) -> None:
    """The gate-run config resolver (epic veiny-trout-brink): inside a ``gate_config`` scope a
    gate op gets the run-boundary's resolved config (honoring a caller's explicit config); outside
    a scope it falls back to a fresh ``from_env`` (the standalone-op path). Resolved ONCE at the
    boundary, read uniformly by every op."""
    import dataclasses

    from rebar.llm import config as llm_config

    p = _proj(tmp_path)
    # Outside any scope → a fresh from_env (the default model), never a stale active config.
    assert llm_config.resolve_gate_config(repo_root=str(p)).model == DEFAULT_MODEL
    custom = dataclasses.replace(
        llm_config.LLMConfig.from_env(repo_root=str(p)), model="caller-model-xyz"
    )
    with llm_config.gate_config(custom):
        assert llm_config.resolve_gate_config() is custom  # the boundary config wins
    # scope exited → back to from_env (the custom config never leaks)
    assert llm_config.resolve_gate_config(repo_root=str(p)).model == DEFAULT_MODEL
