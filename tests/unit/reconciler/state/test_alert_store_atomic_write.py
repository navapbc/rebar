"""Local bridge alerts use atomic replacement, create missing parents, and retain exact
bytes with 0o600 mode. They require no ticket commit or push."""

from __future__ import annotations

import importlib
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ALERT_STORE_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "alert_store.py"


def _load_alert_store() -> ModuleType:
    return importlib.import_module("rebar.reconciler.alert_store")


@pytest.fixture
def alert_store() -> ModuleType:
    mod = _load_alert_store()
    yield mod
    sys.modules.pop("_alert_store_atomic_write_mod", None)


def test_writes_exact_bytes(tmp_path: Path, alert_store: ModuleType) -> None:
    """The destination holds exactly the passed string — no added newline / re-encoding."""
    target = tmp_path / "bridge_alerts" / "state.json"
    content = '{"records": {"k": {"resolved": false}}}'  # no trailing newline on purpose
    alert_store._atomic_write(target, content)
    assert target.read_bytes() == content.encode("utf-8")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file-mode semantics")
def test_mode_is_0o600(tmp_path: Path, alert_store: ModuleType) -> None:
    """The written file keeps the historical 0o600 permission bits."""
    target = tmp_path / "bridge_alerts" / "state.json"
    alert_store._atomic_write(target, "{}")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_creates_missing_parent_dir(tmp_path: Path, alert_store: ModuleType) -> None:
    """A missing parent directory is created (retained explicit mkdir before the seam)."""
    target = tmp_path / "deep" / "nested" / "bridge_alerts" / "state.json"
    assert not target.parent.exists()
    alert_store._atomic_write(target, "{}")
    assert target.read_text(encoding="utf-8") == "{}"


def test_replaces_existing_atomically(tmp_path: Path, alert_store: ModuleType) -> None:
    """An existing destination is fully replaced (no partial/truncated content)."""
    target = tmp_path / "bridge_alerts" / "state.json"
    target.parent.mkdir(parents=True)
    target.write_text("OLD-CONTENT-LONGER-THAN-NEW", encoding="utf-8")
    alert_store._atomic_write(target, "NEW")
    assert target.read_text(encoding="utf-8") == "NEW"
