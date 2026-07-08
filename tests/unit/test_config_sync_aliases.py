"""EV-1: unified sync model REBAR_SYNC_PUSH / REBAR_SYNC_PULL with the permanent
REBAR_NO_SYNC alias and its negative->positive boolean flip. Verifies the canonical
names, the REBAR_NO_SYNC alias (warn + map), canonical-wins precedence, and the value
flip — at the config layer (the single source of truth) and through the push
(_push_mode) / freshness (_sync_disabled) consumers.

(The scheduled REBAR_PUSH alias of REBAR_SYNC_PUSH was removed pre-1.0 — DE7 — so it
is now ignored; only REBAR_SYNC_PUSH is honored.)
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
        "REBAR_SYNC_PUSH",
        "REBAR_SYNC_PULL",
        "REBAR_NO_SYNC",
    ):
        monkeypatch.delenv(name, raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir()
    (p / ".git").mkdir()
    return p


# ── canonical env vars ────────────────────────────────────────────────────────
def test_canonical_sync_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_SYNC_PUSH", "async")
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    c = cfg.load_config(root=_proj(tmp_path))
    assert c.sync.push == "async" and c.sync.pull == "off"


# ── removed alias: REBAR_PUSH is no longer read (DE7) ─────────────────────────
def test_removed_rebar_push_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # REBAR_PUSH was removed pre-1.0; it no longer feeds sync.push (which stays at
    # its default 'always'). Only REBAR_SYNC_PUSH is honored now.
    monkeypatch.setenv("REBAR_PUSH", "off")
    assert cfg.load_config(root=_proj(tmp_path)).sync.push == "always"


# ── deprecated alias: REBAR_NO_SYNC -> sync.pull (negative->positive flip) ─────
@pytest.mark.parametrize(
    "no_sync_val,expected_pull",
    [
        # truthy → pull off (sync disabled), per the shared _as_bool convention
        ("1", "off"),
        ("true", "off"),
        ("yes", "off"),
        ("on", "off"),
        ("TRUE", "off"),  # case-insensitive
        (" 1 ", "off"),  # whitespace-tolerant
        # falsy → pull on (sync stays enabled). Regression (bug 7a): the old
        # `value and value != "0"` treated these non-"0" strings as SET, WRONGLY
        # disabling pull and inverting the documented truthy convention.
        ("0", "on"),
        ("", "on"),
        ("false", "on"),
        ("no", "on"),
        ("off", "on"),
        (" 0 ", "on"),  # whitespace-tolerant falsy
        ("FALSE", "on"),  # case-insensitive falsy
    ],
)
def test_legacy_no_sync_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sync_val: str, expected_pull: str
) -> None:
    monkeypatch.setenv("REBAR_NO_SYNC", no_sync_val)
    assert cfg.load_config(root=_proj(tmp_path)).sync.pull == expected_pull


def test_canonical_pull_beats_legacy_no_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REBAR_SYNC_PULL", "on")
    monkeypatch.setenv("REBAR_NO_SYNC", "1")  # legacy ignored when canonical set
    assert cfg.load_config(root=_proj(tmp_path)).sync.pull == "on"


def test_no_sync_warns_deprecated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setenv("REBAR_NO_SYNC", "1")
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        cfg.load_config(root=_proj(tmp_path))
    assert any("REBAR_NO_SYNC" in r.getMessage() for r in caplog.records)


def test_default_sync_when_unset(tmp_path: Path) -> None:
    c = cfg.load_config(root=_proj(tmp_path))
    assert c.sync.push == "always" and c.sync.pull == "on"  # defaults


# ── precedence: env over a config file ────────────────────────────────────────
def test_env_beats_project_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'always'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")  # env beats the file
    assert cfg.load_config(root=p).sync.push == "off"


# ── consumers route through config ────────────────────────────────────────────
def test_push_mode_reads_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar._store import push

    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    assert push._push_mode(str(p)) == "off"
    monkeypatch.setenv("REBAR_SYNC_PUSH", "async")
    cfg.reset_config_cache()
    assert push._push_mode(str(p)) == "async"


def test_sync_disabled_reads_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar._engine_support import reads

    p = _proj(tmp_path)
    assert reads._sync_disabled(str(p)) is False  # default pull=on
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    cfg.reset_config_cache()
    assert reads._sync_disabled(str(p)) is True
    # legacy alias also disables
    monkeypatch.delenv("REBAR_SYNC_PULL")
    monkeypatch.setenv("REBAR_NO_SYNC", "1")
    cfg.reset_config_cache()
    assert reads._sync_disabled(str(p)) is True


# ── CLI flag: --no-pull (canonical; --no-sync alias was removed, ticket 5899) ──
@pytest.mark.parametrize(
    "flags,expected_no_sync",
    [([], False), (["--no-pull"], True)],
)
def test_no_pull_flag_strips_and_opts_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str], expected_no_sync: bool
) -> None:
    """The read dispatcher strips --no-pull before the subcommand and passes the
    opt-out to ensure_fresh; the subcommand never sees the flag."""
    # The read dispatcher (main / _COMMANDS / facade calls) lives in reads_cli; patch
    # there (reads_cli binds the facades at its own import, so patching `reads` wouldn't
    # affect the dispatcher).
    from rebar._engine_support import reads_cli

    seen: dict = {}

    def _fake_fresh(tracker, *, no_sync=False):
        seen["no_sync"] = no_sync

    def _dummy(rest, tracker):
        seen["rest"] = rest
        return 0

    monkeypatch.setattr(reads_cli, "ensure_fresh", _fake_fresh)
    monkeypatch.setattr(reads_cli, "tracker_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setitem(reads_cli._COMMANDS, "dummy", _dummy)
    rc = reads_cli.main(["dummy", "--id", "x", *flags])
    assert rc == 0
    assert seen["no_sync"] is expected_no_sync
    assert seen["rest"] == ["--id", "x"]  # flag stripped, other args intact


def test_no_sync_cli_alias_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deprecated --no-sync CLI alias was removed (ticket 5899): the dispatcher no
    longer recognizes it, so it does NOT opt out of freshness and is passed through to
    the subcommand as an ordinary (unknown) argument rather than being stripped."""
    from rebar._engine_support import reads_cli

    seen: dict = {}

    def _fake_fresh(tracker, *, no_sync=False):
        seen["no_sync"] = no_sync

    def _dummy(rest, tracker):
        seen["rest"] = rest
        return 0

    monkeypatch.setattr(reads_cli, "ensure_fresh", _fake_fresh)
    monkeypatch.setattr(reads_cli, "tracker_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setitem(reads_cli._COMMANDS, "dummy", _dummy)
    rc = reads_cli.main(["dummy", "--id", "x", "--no-sync"])
    assert rc == 0
    assert seen["no_sync"] is False  # not recognized -> no opt-out
    assert seen["rest"] == ["--id", "x", "--no-sync"]  # passed through, not stripped
