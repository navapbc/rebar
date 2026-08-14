#!/usr/bin/env python3
"""Classify one Gerrit patchset as ``docs-only`` or fail closed to ``full``.

This module is executed from a credentials-disabled sparse checkout of trusted
``origin/main``.  The patchset repository is data only: the classifier asks Git for the
NUL-delimited, rename-disabled ``HEAD^..HEAD`` path set and never imports or sources a
file from the patchset.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePosixPath

DOCS_ONLY = "docs-only"
FULL = "full"

# Root-level prose that cannot change executed project or CI behavior. Agent instruction
# files are deliberately absent: AGENTS.md / CLAUDE.md are repository policy, not prose.
ROOT_DOCUMENTATION = frozenset(
    {
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "GOVERNANCE.md",
        "MAINTAINERS.md",
        "README.md",
        "SECURITY.md",
        "SUPPORT.md",
    }
)
DOC_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".md", ".png", ".svg", ".webp"})
ADR_NUMBER_MARKER = re.compile(r"docs/adr/\.numbers/[0-9]{4}")


def _well_formed(path: str) -> bool:
    """Whether *path* is one canonical, repository-relative Git path."""
    if not path or "\\" in path or any(ord(char) < 32 for char in path):
        return False
    parsed = PurePosixPath(path)
    return (
        not parsed.is_absolute()
        and parsed.as_posix() == path
        and all(part not in {"", ".", ".."} for part in parsed.parts)
    )


def is_documentation_path(path: str) -> bool:
    """Return true only for a complete, intentionally narrow documentation allowlist."""
    if not _well_formed(path):
        return False
    if path in ROOT_DOCUMENTATION:
        return True
    if ADR_NUMBER_MARKER.fullmatch(path):
        return True
    if not path.startswith("docs/"):
        return False
    return PurePosixPath(path).suffix.lower() in DOC_SUFFIXES


def classify_paths(paths: Iterable[str]) -> str:
    """Classify a decoded path set; empty, malformed, or unknown means full Verify."""
    materialized = tuple(paths)
    if not materialized or len(set(materialized)) != len(materialized):
        return FULL
    return DOCS_ONLY if all(is_documentation_path(path) for path in materialized) else FULL


def parse_paths0(raw: bytes) -> tuple[str, ...] | None:
    """Parse exact ``git diff --name-only -z`` output, rejecting ambiguity."""
    if not raw or not raw.endswith(b"\0"):
        return None
    encoded_paths = raw[:-1].split(b"\0")
    if not encoded_paths or any(not path for path in encoded_paths):
        return None
    try:
        paths = tuple(path.decode("utf-8", errors="strict") for path in encoded_paths)
    except UnicodeDecodeError:
        return None
    return paths if all(_well_formed(path) for path in paths) else None


def classify_paths0(raw: bytes) -> str:
    """Classify NUL-delimited Git output, defaulting every parse failure to full."""
    paths = parse_paths0(raw)
    return classify_paths(paths) if paths is not None else FULL


def changed_paths(repository: Path) -> tuple[str, ...] | None:
    """Read only the exact patchset commit's paths; unresolved ``HEAD^`` is unknown."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "HEAD^",
                "HEAD",
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return parse_paths0(result.stdout)


DiffReader = Callable[[Path], tuple[str, ...] | None]


def classify_repository(repository: Path, *, diff_reader: DiffReader = changed_paths) -> str:
    """Classify a patchset repository; operational uncertainty completes as ``full``."""
    try:
        paths = diff_reader(repository)
        return classify_paths(paths) if paths is not None else FULL
    except Exception:  # noqa: BLE001 - an unknown error at this trust boundary must fail closed
        # This is deliberately broad at the trust boundary: a normally completing
        # classifier must never turn an unexpected parser/runtime error into docs-only.
        return FULL


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(classify_repository(args.repository))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
