#!/usr/bin/env python3
"""ADR-number uniqueness / bijection gate (story 0743).

Every ADR file ``docs/adr/NNNN-slug.md`` is paired with a per-number marker
``docs/adr/.numbers/NNNN`` whose content is that ADR's filename. Two ADRs claiming
one number produce an add/add marker conflict git must resolve; this script is the
CI backstop that asserts, on the merged tree, that the bijection holds and that no
ADR cross-reference dangles.

API contract:
  - DEFAULT_ADR_DIR: Path                                  # repo docs/adr
  - MARKERS_DIRNAME: str                                   # ".numbers"
  - check(adr_dir: Path, docs_dir: Path | None = None) -> list[str]  # [] == clean
  - main(argv: list[str] | None = None) -> int            # 0 clean, 1 failures
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADR_DIR = REPO_ROOT / "docs" / "adr"

MARKERS_DIRNAME = ".numbers"

# An ADR file: ``NNNN-slug.md`` with a leading 4-digit number.
_ADR_RE = re.compile(r"^(\d{4})-.+\.md$")

# A marker file name: exactly 4 digits.
_MARKER_RE = re.compile(r"^\d{4}$")

# A markdown link target: the ``(target)`` of ``[text](target)``.
_LINK_RE = re.compile(r"\]\(([^)]+)\)")

# A URL with an explicit scheme (http:, https:, mailto:, ...) — never a local file.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")

# An ADR reference by number+slug, optionally ``.md``.
_ADR_REF_RE = re.compile(r"^(\d{4})-.+?(?:\.md)?$")


def _adr_files(adr_dir: Path) -> dict[str, list[str]]:
    """Map ``NNNN`` -> list of ADR filenames carrying that number."""
    by_number: dict[str, list[str]] = defaultdict(list)
    for path in sorted(adr_dir.glob("*.md")):
        m = _ADR_RE.match(path.name)
        if m:
            by_number[m.group(1)].append(path.name)
    return by_number


def _marker_files(adr_dir: Path) -> dict[str, str]:
    """Map marker ``NNNN`` -> its stripped content (the filename it names)."""
    markers_dir = adr_dir / MARKERS_DIRNAME
    markers: dict[str, str] = {}
    if not markers_dir.is_dir():
        return markers
    for path in sorted(markers_dir.iterdir()):
        if path.is_file() and _MARKER_RE.match(path.name):
            markers[path.name] = path.read_text(encoding="utf-8").strip()
    return markers


def _check_bijection(adr_dir: Path) -> list[str]:
    """Rules 1-6: duplicate numbers, markers, and content bijection."""
    errors: list[str] = []
    adr_by_number = _adr_files(adr_dir)
    markers = _marker_files(adr_dir)

    # Rule 1: duplicate number.
    for number, names in sorted(adr_by_number.items()):
        if len(names) > 1:
            errors.append(f"duplicate ADR number {number}: {', '.join(sorted(names))}")

    # Rule 2: missing marker.
    for number in sorted(adr_by_number):
        if number not in markers:
            errors.append(f"ADR {number} has no marker file .numbers/{number}")

    # Rule 3: orphan marker.
    for number in sorted(markers):
        if number not in adr_by_number:
            errors.append(f"orphan marker .numbers/{number} references no ADR numbered {number}")

    # Rules 4 & 5: marker content correctness.
    for number in sorted(markers):
        content = markers[number]
        # Rule 5: marker name must equal the number-prefix of the filename it names.
        referenced_prefix = content[:4]
        if not _MARKER_RE.match(referenced_prefix) or referenced_prefix != number:
            errors.append(
                f"marker .numbers/{number} names {content!r} whose number-prefix"
                f" does not match {number}"
            )
            continue
        # Rule 4: marker content must equal the actual ADR filename for this number.
        actual = adr_by_number.get(number)
        if actual and content not in actual:
            errors.append(
                f"marker .numbers/{number} content {content!r} does not match"
                f" ADR filename {actual[0]!r}"
            )

    # Rule 6: duplicate marker content.
    content_to_numbers: dict[str, list[str]] = defaultdict(list)
    for number, content in markers.items():
        content_to_numbers[content].append(number)
    for content, numbers in sorted(content_to_numbers.items()):
        if len(numbers) > 1:
            errors.append(
                f"duplicate marker content {content!r} shared by markers"
                f" {', '.join(sorted(numbers))}"
            )

    return errors


def _resolve_adr_target(target: str, containing_file: Path, adr_dir: Path) -> str | None:
    """Return the ``NNNN-slug`` stem an ADR link target resolves to, or None to ignore."""
    target = target.strip()
    if not target or _SCHEME_RE.match(target):
        return None
    # Strip anchor and query.
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    while target.startswith("./"):
        target = target[2:]
    if MARKERS_DIRNAME in target:
        return None

    inside_adr = adr_dir in containing_file.parents
    stem: str | None = None
    if "adr/" in target:
        stem = target.rsplit("adr/", 1)[1]
    elif inside_adr and "/" not in target:
        stem = target
    if stem is None:
        return None

    m = _ADR_REF_RE.match(stem)
    if not m:
        return None
    if stem.endswith(".md"):
        stem = stem[: -len(".md")]
    return stem


def _check_references(adr_dir: Path, docs_dir: Path) -> list[str]:
    """Rule 7: every ADR markdown link target under docs/ resolves to an ADR file."""
    errors: list[str] = []
    for md in sorted(docs_dir.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _LINK_RE.finditer(text):
            stem = _resolve_adr_target(m.group(1), md, adr_dir)
            if stem is None:
                continue
            if not (adr_dir / f"{stem}.md").is_file():
                number = stem[:4]
                errors.append(
                    f"dangling ADR reference to {number} ({stem}.md) in {md.relative_to(docs_dir)}"
                )
    return errors


def check(adr_dir: Path, docs_dir: Path | None = None) -> list[str]:
    """Return human-readable error strings; an empty list means the tree is clean."""
    if docs_dir is None:
        docs_dir = adr_dir.parent
    errors: list[str] = []
    errors.extend(_check_bijection(adr_dir))
    errors.extend(_check_references(adr_dir, docs_dir))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adr-dir", type=Path, default=DEFAULT_ADR_DIR)
    parser.add_argument("--docs-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    adr_dir = args.adr_dir
    docs_dir = args.docs_dir if args.docs_dir is not None else adr_dir.parent

    errors = check(adr_dir, docs_dir)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
