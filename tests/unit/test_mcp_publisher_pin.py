"""Held-out oracle for the mcp_verify sha256 check (story 08a8).

The mcp_verify job downloads the pinned mcp-publisher archive and verifies it with
`echo "<pinned-sha256>  <file>" | sha256sum -c --strict` BEFORE the OIDC-privileged
mcp_registry job ever sees it. This pins that the verify mechanism actually rejects a
corrupted/substituted archive (a wrong byte -> non-zero exit), so an unverified archive is
never extracted or executed.

Tests assert OBSERVABLE behaviour: the exit code of the real `sha256sum -c` check.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest


def _has_gnu_sha256sum() -> bool:
    """release.yml runs on ubuntu-latest (GNU coreutils sha256sum, which supports `-c
    --strict`). macOS ships a Darwin `sha256sum` that lacks check mode, so this test is
    meaningful only where GNU coreutils is present — skip elsewhere rather than false-fail."""
    if shutil.which("sha256sum") is None:
        return False
    try:
        out = subprocess.run(["sha256sum", "--version"], capture_output=True, text=True)
        return "coreutils" in (out.stdout + out.stderr).lower()
    except OSError:
        return False


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _verify(archive: Path, expected_sha: str) -> subprocess.CompletedProcess:
    # Replicates the mcp_verify step exactly: `echo "<sha>  <file>" | sha256sum -c --strict`.
    return subprocess.run(
        ["sha256sum", "-c", "--strict"],
        input=f"{expected_sha}  {archive.name}\n",
        text=True,
        capture_output=True,
        cwd=str(archive.parent),
    )


@pytest.mark.skipif(
    not _has_gnu_sha256sum(), reason="GNU coreutils sha256sum -c required (CI is ubuntu)"
)
def test_verify_passes_on_intact_archive(tmp_path: Path) -> None:
    archive = tmp_path / "mcp-publisher.tar.gz"
    archive.write_bytes(b"pretend-tarball-contents-0123456789")
    cp = _verify(archive, _sha256(archive))
    assert cp.returncode == 0, f"intact archive should verify: {cp.stderr}"


# ── HELD-OUT: a corrupted byte must fail the verify ───────────────────────────
@pytest.mark.skipif(
    not _has_gnu_sha256sum(), reason="GNU coreutils sha256sum -c required (CI is ubuntu)"
)
def test_verify_fails_on_corrupted_byte(tmp_path: Path) -> None:
    archive = tmp_path / "mcp-publisher.tar.gz"
    original = b"pretend-tarball-contents-0123456789"
    archive.write_bytes(original)
    pinned = _sha256(archive)  # pin the digest of the INTACT archive
    # Corrupt exactly one byte.
    corrupted = bytearray(original)
    corrupted[0] ^= 0xFF
    archive.write_bytes(bytes(corrupted))
    cp = _verify(archive, pinned)
    assert cp.returncode != 0, "a corrupted archive must fail sha256sum -c (verify step)"


@pytest.mark.skipif(
    not _has_gnu_sha256sum(), reason="GNU coreutils sha256sum -c required (CI is ubuntu)"
)
def test_verify_fails_on_wrong_pinned_hash(tmp_path: Path) -> None:
    # A stale/typoed pinned hash (right archive, wrong hash) also fails closed.
    archive = tmp_path / "mcp-publisher.tar.gz"
    archive.write_bytes(b"pretend-tarball-contents-0123456789")
    wrong = "0" * 64
    cp = _verify(archive, wrong)
    assert cp.returncode != 0, "a wrong pinned hash must fail the verify step"
