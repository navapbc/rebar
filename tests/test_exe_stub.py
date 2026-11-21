"""The portable tool stub behind ``tests/_exe_stub.py`` (bug transhuman-wounded-pangolin).

These run on every OS. On Windows they are the evidence that a bare-name spawn finds the
stub ahead of the real tool, which a ``#!/bin/sh`` stub never achieves there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _exe_stub import install_exe_stub


@pytest.fixture
def stub_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    real_git = shutil.which("git")
    assert real_git
    install_exe_stub(
        bin_dir,
        "git",
        f"""
        import time
        if sys.argv[1:2] == ["stubbed"]:
            print("from the stub")
            sys.exit(7)
        if sys.argv[1:2] == ["hang"]:
            time.sleep(60)
        exec_real([{real_git!r}, *sys.argv[1:]])
        """,
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def test_a_bare_name_spawn_runs_the_stub(stub_path: Path) -> None:
    proc = subprocess.run(["git", "stubbed"], capture_output=True, text=True, check=False)
    assert (proc.returncode, proc.stdout.strip()) == (7, "from the stub")


def test_exec_real_passes_output_and_status_through(stub_path: Path) -> None:
    version = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
    assert version.stdout.startswith("git version")
    bad = subprocess.run(["git", "no-such-subcommand"], capture_output=True, text=True, check=False)
    assert bad.returncode != 0
    assert "no-such-subcommand" in bad.stderr


def test_a_timeout_kill_ends_the_stub_and_releases_its_pipes(stub_path: Path) -> None:
    """A watchdog kill must not leave the body holding the captured pipes open.

    ``subprocess.run`` kills the process it started, then reads the pipes to EOF. If the
    body outlived that kill (on Windows it runs under the launcher), the read would wait
    for the 60 s sleep."""
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(["git", "hang"], capture_output=True, timeout=2, check=False)
    # timing: hang-guard — the body sleeps 60 s, so 30 s is exceeded only if the kill missed it
    assert time.monotonic() - start < 30


def test_the_stub_name_matches_what_the_platform_looks_up(stub_path: Path) -> None:
    expected = "git.exe" if sys.platform == "win32" else "git"
    assert [p.name for p in stub_path.iterdir()] == [expected]
    # Windows matches PATHEXT case-insensitively and may report ``git.EXE``.
    found = shutil.which("git")
    assert found is not None
    assert os.path.normcase(found) == os.path.normcase(str(stub_path / expected))
