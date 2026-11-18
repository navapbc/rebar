"""The POSIX-shell probe behind ``requires_posix_shell`` (bug citreous-talented-wildebeest).

Tests that run ``bash`` or execute shebang stubs are skipped on hosts without a POSIX shell (the
Windows sweep). The probe must say no where the shell is missing and must never fire on a POSIX
host, or Linux CI would silently stop running those tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _sandbox_capabilities import posix_shell_available, requires_posix_shell


def test_a_missing_shell_is_not_available(tmp_path: Path) -> None:
    assert posix_shell_available(str(tmp_path / "no-such-shell")) is False


def test_an_executable_script_is_available(tmp_path: Path) -> None:
    sh = tmp_path / "sh"
    sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sh.chmod(0o755)
    assert posix_shell_available(str(sh)) is True


def test_a_non_executable_file_is_not_available(tmp_path: Path) -> None:
    sh = tmp_path / "sh"
    sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sh.chmod(0o644)
    if os.access(sh, os.X_OK):
        pytest.skip("host does not honour POSIX execute bits")
    assert posix_shell_available(str(sh)) is False


def test_the_mark_does_not_skip_where_bin_sh_exists() -> None:
    if not os.path.exists("/bin/sh"):
        pytest.skip("no /bin/sh on this host; the mark is meant to skip here")
    assert requires_posix_shell.args == (False,)
