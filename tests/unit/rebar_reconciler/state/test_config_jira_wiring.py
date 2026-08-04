"""0ac6 (slice 3): route jira.* (url/user/project) through the typed Config so a
`[tool.rebar.jira]` / `rebar.toml` value is actually CONSUMED by the reconciler,
with the Atlassian-standard env vars JIRA_URL / JIRA_USER / JIRA_PROJECT as the
canonical (no-warn) env override. The SECRET JIRA_API_TOKEN stays env-ONLY (never a
config key).

Exercises the shared resolver acli_subprocess.resolve_jira_settings() across config
LOCATIONS (pyproject, rebar.toml, XDG user, env, `rebar -c`), asserting precedence
CLI > env > project > user > default, project_default substitution, the env-only
token, ConfigError → env fallback, and provenance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg
from rebar_reconciler.adapters.jira.acli_subprocess import resolve_jira_settings

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


# ── defaults / project_default ────────────────────────────────────────────────
def test_defaults_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    s = resolve_jira_settings()
    assert (s.url, s.user, s.project, s.api_token) == ("", "", "", "")


def test_project_default_applies_only_when_empty(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert resolve_jira_settings(project_default="DIG").project == "DIG"
    (p / "rebar.toml").write_text("[jira]\nproject = 'TEAM'\n", encoding="utf-8")
    cfg.reset_config_cache()
    assert resolve_jira_settings(project_default="DIG").project == "TEAM"  # file wins


# ── config-file locations are consumed ────────────────────────────────────────
def test_pyproject(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text(
        "[tool.rebar.jira]\nurl = 'https://pp.example'\nuser = 'pp@x'\nproject = 'PP'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    s = resolve_jira_settings()
    assert (s.url, s.user, s.project) == ("https://pp.example", "pp@x", "PP")


def test_rebar_toml(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[jira]\nurl = 'https://rt.example'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert resolve_jira_settings().url == "https://rt.example"


def test_rebar_toml_alt_url(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[jira]\nurl = 'https://alt.example'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert resolve_jira_settings().url == "https://alt.example"


# ── canonical env name is the unprefixed Atlassian name ───────────────────────
def test_canonical_env_is_unprefixed(tmp_path: Path, monkeypatch) -> None:
    """JIRA_URL (not REBAR_JIRA_URL) is the canonical env override of jira.url."""
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    monkeypatch.setenv("JIRA_URL", "https://env.example")
    monkeypatch.setenv("REBAR_JIRA_URL", "https://wrong.example")  # NOT the canonical name
    assert resolve_jira_settings().url == "https://env.example"


# ── precedence CLI > env > project > user > default ───────────────────────────
def test_precedence_full_chain(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    user_base = _xdg(tmp_path, "[jira]\nurl = 'https://user.example'\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_base))
    monkeypatch.setenv("REBAR_ROOT", str(p))
    # user only
    assert resolve_jira_settings().url == "https://user.example"
    # project beats user
    (p / "rebar.toml").write_text("[jira]\nurl = 'https://project.example'\n", encoding="utf-8")
    cfg.reset_config_cache()
    assert resolve_jira_settings().url == "https://project.example"
    # env beats project
    monkeypatch.setenv("JIRA_URL", "https://env.example")
    cfg.reset_config_cache()
    assert resolve_jira_settings().url == "https://env.example"
    # cli beats env
    cfg.set_cli_overrides(cfg.parse_cli_overrides(["jira.url=https://cli.example"]))
    assert resolve_jira_settings().url == "https://cli.example"
    cfg.set_cli_overrides(None)


# ── the SECRET token is env-only ──────────────────────────────────────────────
def test_api_token_env_only(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    # A config file MUST NOT be able to set the secret (api_token is not a jira key).
    (p / "rebar.toml").write_text(
        "[jira]\nurl = 'https://x'\napi_token = 'leaked-via-file'\n", encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert resolve_jira_settings().api_token == ""  # file value ignored (unknown key warns)
    monkeypatch.setenv("JIRA_API_TOKEN", "secret-from-env")
    cfg.reset_config_cache()
    assert resolve_jira_settings().api_token == "secret-from-env"


# ── malformed config degrades to env-only (does not break a reconcile) ────────
def test_malformed_config_falls_back_to_env(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[jira]\nurl = [ broken toml\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    monkeypatch.setenv("JIRA_URL", "https://envfallback.example")
    monkeypatch.setenv("JIRA_USER", "fallback@x")
    assert resolve_jira_settings().url == "https://envfallback.example"
    assert resolve_jira_settings().user == "fallback@x"


# ── a non-https jira.url FAILS LOUD (parity with DC), NOT env fallback ─────────
def test_non_https_jira_url_fails_loud_not_env_fallback(tmp_path: Path, monkeypatch) -> None:
    """bug bdb8: a WELL-FORMED config whose EFFECTIVE jira.url is cleartext is a deliberate
    security-policy rejection (InsecureUrlError), so resolve_jira_settings must let it
    PROPAGATE — unlike a malformed config, which degrades to env. This is the fail-loud
    posture the DC resolver already has.

    (An env JIRA_URL would OVERRIDE the file by precedence — env > file — so the http value
    must be the effective one here, i.e. no overriding env is set.)

    MUTATION-CHECK (inverse): drop the ``except InsecureUrlError: raise`` re-raise in
    resolve_jira_settings → the rejection is swallowed and this test goes RED (it would
    degrade to the env values instead of raising).
    """
    from rebar.config import InsecureUrlError

    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[jira]\nurl = 'http://insecure.example'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    with pytest.raises(InsecureUrlError, match="not 'https'"):
        resolve_jira_settings()


def test_non_https_jira_url_resolves_when_allow_insecure(tmp_path: Path, monkeypatch) -> None:
    """The override: jira.allow_insecure=true downgrades the rejection to a warning, so the
    cleartext url resolves (loopback/trusted-network escape hatch)."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[jira]\nurl = 'http://localhost:2990'\nallow_insecure = true\n", encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert resolve_jira_settings().url == "http://localhost:2990"


def test_dc_resolver_also_fails_loud_on_non_https_jira_url(tmp_path: Path, monkeypatch) -> None:
    """The DC settings resolver reads config.jira.project, so building the typed Config
    validates jira.url too — a non-https jira.url raises InsecureUrlError from the DC path
    as well (consistent with its documented fail-loud posture)."""
    from rebar.config import InsecureUrlError
    from rebar_reconciler.adapters.jira_datacenter.settings import (
        resolve_jira_datacenter_settings,
    )

    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[jira]\nurl = 'http://insecure.example'\n[reconciler]\nbase_url = 'https://dc.example'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    with pytest.raises(InsecureUrlError, match="not 'https'"):
        resolve_jira_datacenter_settings()


# ── provenance: `rebar config` reports the winning layer ──────────────────────
def test_provenance(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[jira]\nurl = 'https://file.example'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    monkeypatch.setenv("JIRA_URL", "https://env.example")
    config, sources, _ = cfg.resolve_with_sources(root=p)
    assert config.jira.url == "https://env.example"
    assert sources["jira"]["url"] == "env"


# ── consumer: the bootstrap client builder requires the resolved essentials ───
def test_build_acli_client_requires_resolved_url_user_token(tmp_path: Path, monkeypatch) -> None:
    from rebar_reconciler import _attestation

    p = _proj(tmp_path)
    # url/user from the config FILE, token from env → all three present → builds.
    (p / "rebar.toml").write_text(
        "[jira]\nurl = 'https://j.example'\nuser = 'u@x'\n", encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    client = _attestation.build_acli_client_from_env()
    assert client.jira_url == "https://j.example"
    assert client.jira_project == "DIG"  # project_default applied (file left it empty)
    # Missing the env-only token → RuntimeError naming JIRA_API_TOKEN.
    monkeypatch.delenv("JIRA_API_TOKEN")
    cfg.reset_config_cache()
    with pytest.raises(RuntimeError, match="JIRA_API_TOKEN"):
        _attestation.build_acli_client_from_env()
