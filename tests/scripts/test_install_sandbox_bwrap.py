"""Regression tests for readable sandbox apt failures (ticket 7fd3)."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.scripts

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "install_sandbox_bwrap.sh"


def _write_fake_apt(tmp_path: Path, *, fail_install: bool) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    apt_get = bin_dir / "apt-get"
    apt_get.write_text(
        f"""#!/bin/sh
case "$1" in
  update)
    echo "quiet success output that should stay hidden"
    exit 0
    ;;
  install)
    if {"true" if fail_install else "false"}; then
      echo "E: Unable to locate package $4" >&2
      exit 100
    fi
    echo "install success output that should stay hidden"
    exit 0
    ;;
esac
echo "unexpected apt-get invocation: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    apt_get.chmod(apt_get.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _run(tmp_path: Path, *, fail_install: bool) -> subprocess.CompletedProcess[str]:
    bin_dir = _write_fake_apt(tmp_path, fail_install=fail_install)
    return subprocess.run(
        ["bash", str(SCRIPT), "bubblewrap"],
        env=subprocess_env({"PATH": f"{bin_dir}:/usr/bin:/bin"}),
        capture_output=True,
        text=True,
        check=False,
    )


def test_failed_install_surfaces_the_apt_error_tail(tmp_path: Path) -> None:
    result = _run(tmp_path, fail_install=True)

    assert result.returncode == 100
    assert "apt-get install bubblewrap failed with exit 100" in result.stderr
    assert "E: Unable to locate package bubblewrap" in result.stderr


def test_successful_install_stays_quiet(tmp_path: Path) -> None:
    result = _run(tmp_path, fail_install=False)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
