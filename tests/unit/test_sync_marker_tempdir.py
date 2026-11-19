"""The read-fetch throttle marker lives in the platform temp directory (bug leafy-gorillian-gnat).

``ensure_fresh`` throttles its best-effort fetch to once a minute per tracker with a marker
file. The marker used to sit in a literal ``/tmp`` directory, which on Windows is
the drive-relative ``\\tmp\\...``: the directory does not exist, every marker read failed and
every read fetched. These tests pin the marker to ``tempfile.gettempdir()`` by redirecting the
stdlib temp directory, which a literal ``/tmp`` path ignores, so they fail on any host while
the literal remains.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

import pytest

from rebar._engine_support import reads

pytestmark = pytest.mark.unit

_TRACKER = os.path.join(os.sep, "srv", "checkout", ".tickets-tracker")


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "platform-temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _expected_name(tracker_abs: str) -> str:
    return ".ticket-sync-" + hashlib.md5(tracker_abs.encode()).hexdigest()[:12]


def test_the_marker_path_is_inside_the_platform_temp_directory(temp_root: Path) -> None:
    marker = Path(reads.sync_marker_path(_TRACKER))
    assert marker.parent == temp_root
    assert marker.name == _expected_name(_TRACKER)


def test_a_marker_stamped_in_the_temp_directory_reads_back_as_fresh(temp_root: Path) -> None:
    now = int(time.time())
    (temp_root / _expected_name(_TRACKER)).write_text(str(now - 5), encoding="utf-8")

    marker, age = reads._sync_marker_age(_TRACKER, now)

    assert Path(marker).parent == temp_root
    assert age == 5


def test_a_missing_marker_reports_an_age_past_any_throttle_window(temp_root: Path) -> None:
    _marker, age = reads._sync_marker_age(_TRACKER, int(time.time()))
    assert age == 9999
