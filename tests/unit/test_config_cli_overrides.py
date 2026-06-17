"""The CLI `cli` precedence layer (ticket cdd4): `rebar -c section.key=value` (git -c
style) installs a process-wide highest-precedence override that every config consumer
honors. Validates parse, precedence (cli > env > file), the transparency provenance,
a real consumer, and isolation/reset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for sect, keys in cfg._SECTIONS.items():
        for key in keys:
            monkeypatch.delenv(f"REBAR_{sect.upper()}_{key.upper()}", raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


# ── parse ─────────────────────────────────────────────────────────────────────
def test_parse_cli_overrides_nested() -> None:
    assert cfg.parse_cli_overrides(["sync.push=off", "compact.threshold=3"]) == {
        "sync": {"push": "off"},
        "compact": {"threshold": "3"},
    }


def test_parse_cli_overrides_value_may_contain_equals() -> None:
    assert cfg.parse_cli_overrides(["jira.url=https://x/?a=b"]) == {
        "jira": {"url": "https://x/?a=b"}
    }


@pytest.mark.parametrize("bad", ["noequals", "nodot=1", "=1"])
def test_parse_cli_overrides_rejects_malformed(bad: str) -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.parse_cli_overrides([bad])


# ── precedence: cli > env > file ──────────────────────────────────────────────
def test_cli_override_layer_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'always'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "async")  # env beats file...
    cfg.set_cli_overrides({"sync": {"push": "off"}})  # ...cli beats env
    c = cfg.load_config(root=p)
    assert c.sync.push == "off"


def test_cli_override_provenance(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    cfg.set_cli_overrides({"compact": {"threshold": "9"}})
    config, sources, _ = cfg.resolve_with_sources(root=p)
    assert config.compact.threshold == 9 and sources["compact"]["threshold"] == "cli"


def test_explicit_arg_beats_global(tmp_path: Path) -> None:
    cfg.set_cli_overrides({"sync": {"push": "off"}})
    # an explicit cli_overrides= arg takes precedence over the process global
    assert (
        cfg.load_config(root=_proj(tmp_path), cli_overrides={"sync": {"push": "async"}}).sync.push
        == "async"
    )


def test_reset_clears_global(tmp_path: Path) -> None:
    cfg.set_cli_overrides({"sync": {"push": "off"}})
    cfg.reset_config_cache()
    assert cfg.load_config(root=_proj(tmp_path)).sync.push == "always"  # default restored


# ── a real consumer honors the global ─────────────────────────────────────────
def test_push_mode_honors_cli_override(tmp_path: Path) -> None:
    from rebar._store import push

    p = _proj(tmp_path)
    cfg.set_cli_overrides({"sync": {"push": "off"}})
    assert push._push_mode(str(p)) == "off"


# ── end-to-end through the CLI dispatcher ─────────────────────────────────────
def test_cli_dash_c_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    import json

    from rebar._cli import main

    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'always'\n", encoding="utf-8")
    rc = main(["-c", "sync.push=off", "config", "--root", str(p), "--output", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["config"]["sync"]["push"] == "off"
    assert payload["sources"]["sync"]["push"] == "cli"


def test_cli_dash_c_malformed_is_clean_error(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from rebar._cli import main

    rc = main(["-c", "badformat", "config", "--root", str(_proj(tmp_path))])
    err = capsys.readouterr().err
    assert rc == 1 and "Error:" in err and "Traceback" not in err
