#!/usr/bin/env python3
"""Config-is-read DET check (ticket 6754): every _config_schema.py dataclass field
must have at least one attribute-read site outside the schema/config plumbing, or
carry a ``# read-via: <pointer>`` escape marker.

API contract:
  - check(schema_path: Path, root: Path) -> list[str]
  - main(argv: list[str] | None = None) -> int
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = REPO_ROOT / "src" / "rebar" / "_config_schema.py"
DEFAULT_ROOT = REPO_ROOT / "src" / "rebar"

# Matches ``# read-via: <pointer>`` — group(1) is the pointer (may be empty/whitespace).
_MARKER_RE = re.compile(r"#\s*read-via:(.*)")

# Plumbing filenames excluded from the read-site scan.
_PLUMBING = {"_config_schema.py", "config.py"}


def _is_dataclass(node: ast.ClassDef) -> bool:
    """Return True if the class has a @dataclass (or @dataclasses.dataclass) decorator."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "dataclass":
            return True
        if isinstance(dec, ast.Call):
            inner = dec.func
            if isinstance(inner, ast.Name) and inner.id == "dataclass":
                return True
            if isinstance(inner, ast.Attribute) and inner.attr == "dataclass":
                return True
        if isinstance(dec, ast.Attribute) and dec.attr == "dataclass":
            return True
    return False


def _collect_fields(schema_path: Path) -> list[tuple[str, str, int]]:
    """Return ``[(class_name, field_name, lineno), ...]`` for every AnnAssign field
    in a ``@dataclass`` class."""
    try:
        tree = ast.parse(schema_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    fields: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _is_dataclass(node):
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fields.append((node.name, item.target.id, item.lineno))
    return fields


def _check_marker(lines: list[str], lineno: int) -> tuple[bool, str]:
    """Check for a ``# read-via:`` marker on the field's own line or the preceding line.

    Returns ``(found, pointer)`` where ``found`` is True when a marker is present,
    and ``pointer`` is the text after the colon (stripped). When no marker is found,
    ``found`` is False and ``pointer`` is ``""``.
    """
    candidates = [lineno - 1]  # own line (0-indexed)
    if lineno >= 2:
        candidates.append(lineno - 2)  # immediately preceding line (0-indexed)
    for idx in candidates:
        if idx < 0 or idx >= len(lines):
            continue
        m = _MARKER_RE.search(lines[idx])
        if m:
            return True, m.group(1).strip()
    return False, ""


def _collect_reads(root: Path) -> set[str]:
    """Return the set of terminal attribute names read (Load context) in all non-plumbing
    ``*.py`` files under ``root``."""
    reads: set[str] = set()
    for py in root.rglob("*.py"):
        if py.name in _PLUMBING:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
            ):
                reads.add(node.attr)
    return reads


def check(schema_path: Path, root: Path) -> list[str]:
    """Return a list of error strings; ``[]`` means the tree is clean."""
    fields = _collect_fields(schema_path)
    if not fields:
        return []

    source_lines = schema_path.read_text(encoding="utf-8").splitlines()
    reads = _collect_reads(root)
    errors: list[tuple[int, str]] = []  # (lineno, message) for sorting

    for cls_name, field_name, lineno in fields:
        found, pointer = _check_marker(source_lines, lineno)
        if found:
            if not pointer:
                errors.append((
                    lineno,
                    f"{schema_path}:{lineno}: {cls_name}.{field_name}: bare "
                    f"'# read-via:' marker — a pointer/reason is required "
                    f"(e.g. '# read-via: path/to/reader.py description').",
                ))
            # valid marker → ok
        else:
            if field_name in reads:
                pass  # at least one read site found
            else:
                errors.append((
                    lineno,
                    f"{schema_path}:{lineno}: {cls_name}.{field_name} has no "
                    f"attribute-read site outside the schema/config plumbing. "
                    f"Add a read site, or add a '# read-via: <pointer>' marker "
                    f"on the field line (or the immediately preceding line) to "
                    f"document where it is consumed.",
                ))

    errors.sort(key=lambda t: t[0])
    return [msg for _, msg in errors]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check every _config_schema.py dataclass field has a read site.",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="Path to _config_schema.py (default: %(default)s)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="Source root to scan for read sites (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    errors = check(args.schema, args.root)
    if errors:
        for err in errors:
            print(err)
        print(
            f"\n{len(errors)} field(s) lack a read site. "
            f"Add a '# read-via: <pointer>' marker to document indirect access."
        )
        return 1

    print(f"Config-read gate: OK ({args.schema.name} — all dataclass fields are read).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
