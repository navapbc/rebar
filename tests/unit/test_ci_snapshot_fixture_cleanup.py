"""Regression coverage for recursively removable pytest temp trees."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_SNAPSHOT_TEST = Path(__file__).parent / "workflow" / "test_snapshot.py"


def test_completed_snapshot_test_leaves_basetemp_removable(tmp_path: Path) -> None:
    basetemp = tmp_path / "nested-basetemp"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{_SNAPSHOT_TEST}::test_snapshot_is_read_only",
            "--basetemp",
            str(basetemp),
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert basetemp.is_dir()

    shutil.rmtree(basetemp)
    assert not basetemp.exists()
