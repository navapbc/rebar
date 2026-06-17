"""EV-3b: rename TICKETS_TRACKER_DIR -> REBAR_TRACKER_DIR (alias window).
The relocated/decoupled store dir is a supported feature: prefer the REBAR_-prefixed
name, honor the old name during the deprecation window with a one-time warning.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rebar import config as cfg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ROOT", raising=False)


def test_override_none_when_unset() -> None:
    assert cfg.tracker_dir_override() is None


def test_override_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_TRACKER_DIR", "/tmp/canon-tracker")
    assert cfg.tracker_dir_override() == "/tmp/canon-tracker"


def test_override_legacy_alias_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TICKETS_TRACKER_DIR", "/tmp/legacy-tracker")
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        assert cfg.tracker_dir_override() == "/tmp/legacy-tracker"
        cfg.tracker_dir_override()  # second call
        cfg.tracker_dir_override()  # third call
    warns = [r for r in caplog.records if "TICKETS_TRACKER_DIR" in r.getMessage()]
    assert len(warns) == 1  # warn-once (hot path: no log spam)
    assert "REBAR_TRACKER_DIR" in warns[0].getMessage()


def test_canonical_beats_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_TRACKER_DIR", "/tmp/canon")
    monkeypatch.setenv("TICKETS_TRACKER_DIR", "/tmp/legacy")
    assert cfg.tracker_dir_override() == "/tmp/canon"


def test_tracker_dir_uses_canonical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tmp_path / "store"))
    assert cfg.tracker_dir() == Path(str(tmp_path / "store"))


def test_tracker_dir_uses_legacy_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TICKETS_TRACKER_DIR", str(tmp_path / "old"))
    assert cfg.tracker_dir() == Path(str(tmp_path / "old"))


def test_tracker_dir_default_when_no_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    assert cfg.tracker_dir() == tmp_path.resolve() / ".tickets-tracker"


def test_reads_tracker_dir_honors_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._engine_support import reads

    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tmp_path / "rt"))
    assert reads.tracker_dir() == str(tmp_path / "rt")
