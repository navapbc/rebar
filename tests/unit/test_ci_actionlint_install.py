"""CI-reliability guard: ``make actionlint-bin`` must FAIL-FAST on a bad/corrupt download.

Recurring CI flake (Gerrit-verify run 29619392619, job 88011385914): the ``actionlint-bin``
recipe chained its download / checksum / extract steps with ``;`` and ended in an
unconditional ``echo "...installed"``, so a transient GitHub download failure (a ``curl``
error) was MASKED as success — the recipe returned exit 0 with **no binary**, the caller then
ran the missing binary and died with a confusing ``exit 2``, and the pinned-``sha256sum``
verification never actually gated (its failure was ignored too).

This is the guard for the fix: a failed download / checksum / extract must abort the recipe
NON-ZERO and install nothing, so a transient blip surfaces as a clear failure (and is retried),
never a false "installed".
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
        env={"PATH": _SANE_PATH, "HOME": os.environ.get("HOME", "/tmp")},
        capture_output=True,
        text=True,
        timeout=180,
    )
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
