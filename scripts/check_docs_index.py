#!/usr/bin/env python3
"""Docs-index completeness / dead-link checker (ticket f088) — STUB.

API contract (implemented by the held-out TDD loop):
  - find_unindexed(docs_dir) -> list[str]
  - find_broken_links(docs_dir) -> list[tuple[str, str]]
  - main(argv) -> int
  - DEFAULT_DOCS_DIR: Path
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS_DIR = REPO_ROOT / "docs"

INDEX_NAME = "README.md"

# Matches a markdown link target: the ``(target)`` of ``[text](target)``.
# Captures everything up to the closing paren (link targets don't contain ')').
_LINK_RE = re.compile(r"\]\(([^)]+)\)")

# A URL with an explicit scheme (http:, https:, mailto:, etc.) — never a local file.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _link_targets(text: str) -> list[str]:
    """Return the raw targets of every ``](target)`` markdown link in ``text``."""
    return [m.group(1).strip() for m in _LINK_RE.finditer(text)]


def find_unindexed(docs_dir: Path) -> list[str]:
    """Living top-level ``*.md`` files not linked from the index, sorted.

    A file counts as linked only if it appears as a real markdown link target
    (``](name)`` or ``](./name)``); a bare prose mention does not count. The index
    itself and any ``*.local.md`` (git-ignored) file are never reported.
    """
    index_path = docs_dir / INDEX_NAME
    index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    linked: set[str] = set()
    for target in _link_targets(index_text):
        target = target.split("#", 1)[0]
        if target.startswith("./"):
            target = target[2:]
        linked.add(target)

    unindexed: list[str] = []
    for md in docs_dir.glob("*.md"):
        name = md.name
        if name == INDEX_NAME:
            continue
        if md.match("*.local.md"):
            continue
        if name not in linked:
            unindexed.append(name)
    return sorted(unindexed)


def find_broken_links(docs_dir: Path) -> list[tuple[str, str]]:
    """``(source_basename, target)`` for relative markdown links that don't resolve.

    External links (``http:``/``https:``/``mailto:``/any ``scheme:``) are ignored.
    A ``#fragment`` is stripped before resolving; a link with a fragment to an
    existing file is not broken. Targets resolve relative to the linking file's dir.
    """
    docs_root = docs_dir.resolve()
    broken: list[tuple[str, str]] = []
    for md in sorted(docs_dir.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        for target in _link_targets(text):
            if not target or target.startswith("#"):
                continue
            if _SCHEME_RE.match(target):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            resolved = (md.parent / path_part).resolve()
            # Only the integrity of links WITHIN docs/ is this checker's remit;
            # links that reach into the wider repo (``../src``, ``../CONTRIBUTING.md``)
            # are validated by other tooling, not here.
            try:
                resolved.relative_to(docs_root)
            except ValueError:
                continue
            if not resolved.exists():
                broken.append((md.name, target))
    return sorted(broken)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check docs-index completeness and relative-link health."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if there are unindexed docs or broken links",
    )
    parser.parse_args(argv)

    docs_dir = DEFAULT_DOCS_DIR
    unindexed = find_unindexed(docs_dir)
    broken = find_broken_links(docs_dir)

    for name in unindexed:
        sys.stderr.write(f"unindexed doc (not linked from {INDEX_NAME}): {name}\n")
    for source, target in broken:
        sys.stderr.write(f"broken link in {source}: {target}\n")

    return 1 if (unindexed or broken) else 0


if __name__ == "__main__":
    raise SystemExit(main())
