"""0ac6 (slice 1): route ticket_clarity.threshold through the typed Config — it was
the last live `.rebar/config.conf` read bypassing load_config. The typed section name
(`ticket_clarity`) matches the legacy flat key, so the legacy file reads with no alias.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg
from rebar._engine_support import gates

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("REBAR_TICKET_CLARITY_THRESHOLD", raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


# ── typed-Config layer ────────────────────────────────────────────────────────
def test_default(tmp_path: Path) -> None:
    assert cfg.load_config(root=_proj(tmp_path)).ticket_clarity.threshold == 5


def test_pyproject(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text(
        "[tool.rebar.ticket_clarity]\nthreshold = 70\n", encoding="utf-8"
    )
    assert cfg.load_config(root=p).ticket_clarity.threshold == 70


def test_legacy_flat_conf_reads_with_no_alias(tmp_path: Path) -> None:
    """The legacy `.rebar/config.conf` key `ticket_clarity.threshold` maps directly to
    the typed `ticket_clarity` section (matching name) — now via load_config."""
    p = _proj(tmp_path)
    (p / ".rebar").mkdir()
    (p / ".rebar" / "config.conf").write_text("ticket_clarity.threshold=42\n", encoding="utf-8")
    assert cfg.load_config(root=p).ticket_clarity.threshold == 42


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[ticket_clarity]\nthreshold = 70\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_TICKET_CLARITY_THRESHOLD", "9")
    assert cfg.load_config(root=p).ticket_clarity.threshold == 9  # env > file


# ── read_config_file (explicit single-file helper) ────────────────────────────
def test_read_config_file_toml(tmp_path: Path) -> None:
    f = tmp_path / "x.toml"
    f.write_text("[ticket_clarity]\nthreshold = 33\n", encoding="utf-8")
    assert cfg.read_config_file(f).ticket_clarity.threshold == 33


def test_read_config_file_legacy(tmp_path: Path) -> None:
    f = tmp_path / ".rebar" / "config.conf"
    f.parent.mkdir(parents=True)
    f.write_text("ticket_clarity.threshold=44\n", encoding="utf-8")
    assert cfg.read_config_file(f).ticket_clarity.threshold == 44


# ── gates._clarity_threshold consumer ─────────────────────────────────────────
def test_clarity_threshold_implicit(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[ticket_clarity]\nthreshold = 70\n", encoding="utf-8")
    assert gates._clarity_threshold(str(p), None) == 70


def test_clarity_threshold_explicit_file(tmp_path: Path) -> None:
    f = tmp_path / "cfg.toml"
    f.write_text("[ticket_clarity]\nthreshold = 12\n", encoding="utf-8")
    assert gates._clarity_threshold(None, str(f)) == 12


def test_clarity_threshold_default(tmp_path: Path) -> None:
    assert gates._clarity_threshold(str(_proj(tmp_path)), None) == 5


def test_clarity_threshold_failsafe_on_malformed(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text("[tool.rebar] broken === [[\n", encoding="utf-8")
    assert gates._clarity_threshold(str(p), None) == 5  # non-critical gate → default
