"""``rebar config`` transparency command + :func:`rebar.config.resolve_with_sources`:
per-key provenance (cli > env > project > user > default) across representative
COMBINATIONS of config parameters and LOCATIONS, plus the CLI text/JSON rendering
and portability of the output (config-refinement task c647).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar import config as cfg
from rebar._commands import show_config

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


# ── resolve_with_sources: provenance per layer ────────────────────────────────
def test_sources_all_default_when_no_config(tmp_path: Path) -> None:
    cfgobj, sources, project = cfg.resolve_with_sources(root=_proj(tmp_path))
    assert project is None
    assert cfgobj.sync.push == "always"
    # every key attributed to the defaults layer
    assert all(src == "default" for sect in sources.values() for src in sect.values())


def test_sources_project_layer_labeled(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'async'\n", encoding="utf-8")
    cfgobj, sources, project = cfg.resolve_with_sources(root=p)
    assert cfgobj.sync.push == "async"
    assert sources["sync"]["push"] == "project"
    assert sources["sync"]["pull"] == "default"  # untouched key stays default
    assert project is not None and project[1] == "toml"


def test_sources_env_layer_labeled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "42")
    _, sources, _ = cfg.resolve_with_sources(root=_proj(tmp_path))
    assert sources["compact"]["threshold"] == "env"


def test_sources_cli_layer_labeled(tmp_path: Path) -> None:
    _, sources, _ = cfg.resolve_with_sources(
        root=_proj(tmp_path), cli_overrides={"sync": {"push": "off"}}
    )
    assert sources["sync"]["push"] == "cli"


def test_sources_user_layer_labeled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    (xdg / "rebar" / "config.toml").write_text("[compact]\nthreshold = 7\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    _, sources, _ = cfg.resolve_with_sources(root=_proj(tmp_path))
    assert sources["compact"]["threshold"] == "user"


def test_sources_report_winning_layer_under_full_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All five layers present; each key resolves to — and is attributed to — the
    HIGHEST-precedence layer that set it, and the value matches that layer."""
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    # user sets push (will be beaten) + pull (only user sets it -> user wins)
    (xdg / "rebar" / "config.toml").write_text(
        "[sync]\npush = 'async'\npull = 'off'\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    p = _proj(tmp_path)
    # project sets compact.threshold (only project -> project wins)
    (p / "rebar.toml").write_text("[compact]\nthreshold = 25\n", encoding="utf-8")
    # env beats user on sync.push
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    # cli beats env on jira.project
    cfgobj, sources, _ = cfg.resolve_with_sources(
        root=p, cli_overrides={"jira": {"project": "DSO"}}
    )
    assert cfgobj.sync.push == "off" and sources["sync"]["push"] == "env"
    assert cfgobj.sync.pull == "off" and sources["sync"]["pull"] == "user"
    assert cfgobj.compact.threshold == 25 and sources["compact"]["threshold"] == "project"
    assert cfgobj.jira.project == "DSO" and sources["jira"]["project"] == "cli"
    assert cfgobj.scratch.base_dir == "" and sources["scratch"]["base_dir"] == "default"


def test_sources_legacy_conf_labeled_project(tmp_path: Path) -> None:
    """A legacy ``.rebar/config.conf`` is the project layer: its keys attribute to
    'project' and the discovered kind is reported as 'legacy' for transparency."""
    p = _proj(tmp_path)
    (p / ".rebar").mkdir()
    (p / ".rebar" / "config.conf").write_text("sync.push=async\n", encoding="utf-8")
    cfgobj, sources, project = cfg.resolve_with_sources(root=p)
    assert cfgobj.sync.push == "async"
    assert sources["sync"]["push"] == "project"
    assert project is not None and project[1] == "legacy"


def test_sources_match_load_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved Config from resolve_with_sources must equal load_config's — the
    provenance view can never disagree with the live load."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'async'\n[compact]\nthreshold = 9\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    cfgobj, _, _ = cfg.resolve_with_sources(root=p)
    assert cfgobj == cfg.load_config(root=p)


# ── CLI rendering ─────────────────────────────────────────────────────────────
def test_config_cli_text_output(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'async'\n", encoding="utf-8")
    rc = show_config.config_cli(["--root", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sync.push" in out and "async" in out and "[project]" in out
    assert "sync.pull" in out and "[default]" in out
    assert "precedence:" in out and "default < user < project < env < cli" in out


def test_config_cli_text_aligns_long_values(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A long value (jira.url) must not push the [source] column out of alignment —
    every data row's '[' bracket lands in the same column."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[jira]\nurl = 'https://very-long-host.example.atlassian.net'\n", encoding="utf-8"
    )
    show_config.config_cli(["--root", str(p)])
    out = capsys.readouterr().out
    data_rows = [ln for ln in out.splitlines() if " = " in ln and ln.endswith("]")]
    bracket_cols = {ln.index("[") for ln in data_rows}
    assert len(bracket_cols) == 1  # all source brackets aligned to one column


def test_config_cli_json_output(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[compact]\nthreshold = 33\n", encoding="utf-8")
    rc = show_config.config_cli(["--root", str(p), "--output", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["config"]["compact"]["threshold"] == 33
    assert payload["sources"]["compact"]["threshold"] == "project"
    assert payload["project_config"]["kind"] == "toml"
    assert payload["precedence"] == list(cfg.LAYER_ORDER)
    # user config reported as a path + existence flag (machine-specific paths are
    # surfaced explicitly, not hidden — and there's no project here that exists)
    assert payload["user_config"]["exists"] is False


def test_config_cli_reports_config_error_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A strict-mode unknown key surfaces as a clean stderr message + exit 1, not a
    traceback."""
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'off'\nbogus = 1\n", encoding="utf-8")
    rc = show_config.config_cli(["--root", str(p)])
    err = capsys.readouterr().err
    assert rc == 1 and "sync.bogus" in err


def test_config_cli_json_identical_across_clones(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    body = "[sync]\npush = 'async'\n[jira]\nproject = 'DSO'\n"
    results = []
    for name in ("A", "B"):
        r = tmp_path / name
        r.mkdir()
        (r / ".git").mkdir()
        (r / "rebar.toml").write_text(body, encoding="utf-8")
        show_config.config_cli(["--root", str(r), "--output", "json"])
        results.append(json.loads(capsys.readouterr().out))
    a, b = results
    assert a["config"] == b["config"]  # values portable
    assert a["sources"] == b["sources"]  # provenance portable
    assert a["project_config"]["path"] != b["project_config"]["path"]  # only paths differ
