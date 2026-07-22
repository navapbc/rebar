#!/usr/bin/env python3
"""DCO sign-off identity consistency checker (ticket 35d2).

Fails if contributor-facing guidance (AGENTS.md, .agents/rules/*.md,
CONTRIBUTING.md, docs/**/*.md) hardcodes a personal DCO sign-off identity.
Automation-owned paths (infra/, .github/workflows/, tests/, docs/experiments/)
legitimately reference a bot identity and are excluded from the scan.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCOPED_GLOBS = (
    "AGENTS.md",
    ".agents/rules/*.md",
    "CONTRIBUTING.md",
    "docs/**/*.md",
)

EXCLUDED_PREFIXES = (
    "infra/",
    ".github/workflows/",
    "tests/",
    "docs/experiments/",
)

_PATTERN = re.compile(r"joeoakhart\+bot@navapbc\.com|Signed-off-by:\s*Joe Oakhart")


def _scoped_files(root: Path) -> list[Path]:
    seen: set[Path] = set()
    for pattern in SCOPED_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(str(rel).startswith(prefix) for prefix in EXCLUDED_PREFIXES):
                continue
            seen.add(rel)
    return sorted(seen)


def find_violations(root: Path) -> list[tuple[Path, int, str]]:
    """Return (relative_path, line_number, line_text) for each hardcoded match."""
    violations: list[tuple[Path, int, str]] = []
    for rel in _scoped_files(root):
        text = (root / rel).read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _PATTERN.search(line):
                violations.append((rel, line_no, line))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to scan")
    args = parser.parse_args(argv)

    violations = find_violations(Path(args.root))
    if violations:
        for rel, line_no, line in violations:
            print(f"{rel}:{line_no}: hardcoded DCO identity: {line.strip()}", file=sys.stderr)
        print(
            f"check_dco_identity: {len(violations)} hardcoded sign-off identity match(es) "
            "found in contributor-facing guidance",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
