"""Config loader: discovery + TOML/legacy reading + XDG user fallback + the
CLI > env > project > user > defaults layering, verified across representative
COMBINATIONS of config parameters and LOCATIONS, plus precedence + portability
(config-refinement task 43a0).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rebar import config as cfg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient config so each test sees only what it sets up."""
    monkeypatch.delenv("REBAR_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for sect, keys in cfg._SECTIONS.items():
        for key in keys:
            monkeypatch.delenv(f"REBAR_{sect.upper()}_{key.upper()}", raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir()
    (p / ".git").mkdir()  # repo boundary marker
    return p


# ── locations ────────────────────────────────────────────────────────────────
def test_defaults_when_no_config(tmp_path: Path) -> None:
    c = cfg.load_config(root=_proj(tmp_path))
    assert c.sync.push == "always" and c.compact.threshold == 10 and c.mcp.allow_jira_sync is False


def test_project_rebar_toml(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'async'\n[compact]\nthreshold = 25\n", encoding="utf-8")
    c = cfg.load_config(root=p)
    assert c.sync.push == "async" and c.compact.threshold == 25


def test_project_pyproject_tool_rebar(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text(
        "[project]\nname='x'\n[tool.rebar.mcp]\nallow_jira_sync = true\n", encoding="utf-8"
    )
    c = cfg.load_config(root=p)
    assert c.mcp.allow_jira_sync is True


def test_legacy_config_conf_backcompat(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / ".rebar").mkdir()
    (p / ".rebar" / "config.conf").write_text(
        "verify.require_signature_for_close=true\nsync.pull=off\n", encoding="utf-8"
    )
    c = cfg.load_config(root=p)
    assert c.verify.require_signature_for_close is True and c.sync.pull == "off"


def test_rebar_config_env_points_at_explicit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text("[ticket]\ndisplay_mode = 'plain'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_CONFIG", str(elsewhere))
    c = cfg.load_config(root=_proj(tmp_path))
    assert c.ticket.display_mode == "plain"


def test_xdg_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    (xdg / "rebar" / "config.toml").write_text("[compact]\nthreshold = 7\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    c = cfg.load_config(root=_proj(tmp_path))
    assert c.compact.threshold == 7  # user config applied (no project config)


def test_env_var_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_MAX_RETRIES", "9")
    c = cfg.load_config(root=_proj(tmp_path))
    assert c.sync.push == "off" and c.reconciler.lock_max_retries == 9


# ── precedence ───────────────────────────────────────────────────────────────
def test_env_beats_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'async'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    assert cfg.load_config(root=p).sync.push == "off"  # env > project


def test_project_beats_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    (xdg / "rebar" / "config.toml").write_text("[compact]\nthreshold = 5\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[compact]\nthreshold = 99\n", encoding="utf-8")
    assert cfg.load_config(root=p).compact.threshold == 99  # project > user


def test_cli_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    c = cfg.load_config(root=_proj(tmp_path), cli_overrides={"sync": {"push": "async"}})
    assert c.sync.push == "async"  # cli > env


def test_lower_layer_default_does_not_override_higher_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sparse-merge correctness property: a USER config explicitly sets
    sync.push='async'; the PROJECT config sets a DIFFERENT key (sync.pull) but NOT
    sync.push. The project's would-be-default for push must NOT clobber the user's
    explicit value — defaults are applied once, at the end, not per layer."""
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    (xdg / "rebar" / "config.toml").write_text("[sync]\npush = 'async'\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npull = 'off'\n", encoding="utf-8")  # push absent here
    c = cfg.load_config(root=p)
    assert c.sync.push == "async"  # user's explicit survives (project didn't set push)
    assert c.sync.pull == "off"  # project's explicit applies


# ── discovery semantics ──────────────────────────────────────────────────────
def test_upward_discovery_from_subdir(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[compact]\nthreshold = 42\n", encoding="utf-8")
    sub = p / "a" / "b"
    sub.mkdir(parents=True)
    assert cfg.load_config(root=sub).compact.threshold == 42  # found by walking up


def test_discovery_stops_at_git_boundary(tmp_path: Path) -> None:
    # A rebar.toml ABOVE the repo's .git must NOT be picked up from inside the repo.
    (tmp_path / "rebar.toml").write_text("[compact]\nthreshold = 5\n", encoding="utf-8")
    inner = tmp_path / "repo"
    inner.mkdir()
    (inner / ".git").mkdir()
    assert cfg.load_config(root=inner).compact.threshold == 10  # default, not 5


# ── portability ──────────────────────────────────────────────────────────────
def test_portability_same_content_resolves_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same config CONTENT in two different locations (≈ two machines/clones)
    resolves to an identical Config — discovery is repo-root-relative, no absolute
    machine path leaks into the result; and cwd does not affect resolution."""
    body = "[sync]\npush = 'async'\n[jira]\nproject = 'DSO'\n"
    # two independent roots (≈ two machines/clones)
    ra = tmp_path / "A"
    ra.mkdir()
    (ra / ".git").mkdir()
    (ra / "rebar.toml").write_text(body, encoding="utf-8")
    rb = tmp_path / "B"
    rb.mkdir()
    (rb / ".git").mkdir()
    (rb / "rebar.toml").write_text(body, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    ca = cfg.load_config(root=ra)
    monkeypatch.chdir(rb)  # different cwd must not change resolution
    cb = cfg.load_config(root=rb)
    assert ca == cb
    assert ca.sync.push == "async" and ca.jira.project == "DSO"


# ── loud handling in files ───────────────────────────────────────────────────
def test_unknown_key_in_file_warns_but_loads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'off'\nbogus = 1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=p)
    assert c.sync.push == "off"
    assert any("sync.bogus" in r.getMessage() for r in caplog.records)


def test_invalid_value_in_file_fails_closed(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'sometimes'\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(root=p)


# ── additional matrix coverage (combinations flagged in review) ───────────────
def test_rebar_config_env_points_at_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[project]\nname='x'\n[tool.rebar.sync]\npush = 'off'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_CONFIG", str(pp))
    assert cfg.load_config(root=_proj(tmp_path)).sync.push == "off"  # pyproject-kind via $REBAR_CONFIG


def test_env_beats_legacy_conf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / ".rebar").mkdir()
    (p / ".rebar" / "config.conf").write_text("sync.push=async\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    assert cfg.load_config(root=p).sync.push == "off"  # env > legacy project layer


def test_cli_partial_section_merges_with_project(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npull = 'off'\n", encoding="utf-8")  # push absent
    c = cfg.load_config(root=p, cli_overrides={"sync": {"push": "async"}})
    assert c.sync.push == "async" and c.sync.pull == "off"  # both survive across cli/project


def test_rebar_config_env_beats_in_tree_rebar_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[compact]\nthreshold = 99\n", encoding="utf-8")
    other = tmp_path / "other.toml"
    other.write_text("[compact]\nthreshold = 7\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_CONFIG", str(other))
    assert cfg.load_config(root=p).compact.threshold == 7  # explicit $REBAR_CONFIG wins over walk


def test_malformed_pyproject_fails_closed(tmp_path: Path) -> None:
    """A present-but-unparseable pyproject (the would-be config, no rebar.toml) must
    raise ConfigError — NOT be silently skipped — so the security gate fails closed."""
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text("[tool.rebar] this is broken === [[\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(root=p)


def test_absent_pyproject_does_not_preempt_legacy_conf(tmp_path: Path) -> None:
    """A parseable pyproject WITHOUT [tool.rebar] is skipped (not selected), so the
    legacy .rebar/config.conf is still found and applied."""
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (p / ".rebar").mkdir()
    (p / ".rebar" / "config.conf").write_text("compact.threshold=5\n", encoding="utf-8")
    assert cfg.load_config(root=p).compact.threshold == 5
