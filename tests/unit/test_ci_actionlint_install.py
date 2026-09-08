"""Tests for fail-fast actionlint installation.

A download, checksum, or extraction failure must return nonzero, leave no binary, and omit
the success message.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[2]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _child_diag import assert_child_was_not_signal_killed  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]

# A minimal PATH that carries coreutils / curl / tar / make but NOT actionlint, so the recipe
# takes its INSTALL branch (``command -v actionlint`` misses) instead of short-circuiting on a
# pre-installed binary (e.g. a dev box with actionlint on PATH).
_SANE_PATH = "/usr/bin:/bin"


def _has(tool: str) -> bool:
    return shutil.which(tool, path=_SANE_PATH) is not None


@pytest.mark.skipif(
    not all(_has(t) for t in ("make", "curl", "tar", "mktemp")),
    reason="needs make/curl/tar/mktemp on the minimal PATH",
)
def test_actionlint_install_fails_fast_on_download_failure(tmp_path: Path) -> None:
    """A forced-failing download (a nonexistent version → HTTP 404) must make
    ``make actionlint-bin`` exit NON-ZERO and install no binary — not mask the failure as a
    green "installed" (exit 0). RED before the ``set -e`` / ``curl --retry`` fix, where the
    recipe returned 0 with no binary."""
    local_bin = tmp_path / "bin"
    proc = subprocess.run(
        [
            "make",
            "-C",
            str(_ROOT),
            "actionlint-bin",
            "ACTIONLINT_VERSION=0.0.0-nonexistent-rebar-debug",
            f"LOCAL_BIN={local_bin}",
        ],
        # REBAR_ROOT is pinned because this hand-built env drops the tier's inherited
        # isolation root; without it the child falls back to the checkout.
        env={
            "PATH": _SANE_PATH,
            "HOME": os.environ.get("HOME", "/tmp"),
            "REBAR_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    # A signal-killed child also returns nonzero with empty output. Reject that case before
    # checking the installation failure.
    assert_child_was_not_signal_killed(proc, what="the actionlint-bin recipe")
    assert proc.returncode != 0, (
        "actionlint-bin masked a failed download as success (exit 0) — it must fail-fast.\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert not (local_bin / "actionlint").exists(), (
        "no binary must be installed when the download fails"
    )
    assert "actionlint: installed" not in proc.stdout, (
        "recipe printed the false-success 'installed' message despite a failed download"
    )
