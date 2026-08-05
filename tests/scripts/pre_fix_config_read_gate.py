"""Frozen snapshot of the PRE-FIX config-read gate (bug 2c58-e710-275e-4ff7).

This is not live code and nothing in `src/` imports it. It exists so the RED half of
the RED->GREEN proof for bug 2c58 is a PERMANENT in-repo artifact rather than a
development-time observation that vanished with the session.

What it preserves: `_collect_reads` returning a GLOBAL `set[str]` of terminal
attribute names read anywhere under the source root, and a `check()` whose
membership test is the bare `field_name in reads`. That is the defect -- a field was
satisfied by any same-named attribute on any unrelated object, so the gate caught an
inert field only when its name happened to be globally unique.

`tests/scripts/test_check_config_reads_red_proof.py` runs this snapshot and the live
gate against the SAME synthetic schema and asserts the snapshot passes the inert
field while the live gate fires on it. If someone ever regresses the resolver back to
name matching, that test goes red.

Copied verbatim (modulo the docstrings) from `scripts/check_config_reads.py` as it
stood at the commit immediately before the 2c58 fix. DO NOT "fix" this file -- its
whole value is that it still contains the bug.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_MARKER_RE = re.compile(r"#\s*read-via:(.*)")
_PLUMBING = {"_config_schema.py", "config.py"}


def _is_dataclass(node: ast.ClassDef) -> bool:
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
    try:
        tree = ast.parse(schema_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    fields: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_dataclass(node):
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fields.append((node.name, item.target.id, item.lineno))
    return fields


def _check_marker(lines: list[str], lineno: int) -> tuple[bool, str]:
    candidates = [lineno - 1]
    if lineno >= 2:
        candidates.append(lineno - 2)
    for idx in candidates:
        if idx < 0 or idx >= len(lines):
            continue
        m = _MARKER_RE.search(lines[idx])
        if m:
            return True, m.group(1).strip()
    return False, ""


def _collect_reads(root: Path) -> set[str]:
    """THE DEFECT: one global set of terminal attribute NAMES, receiver discarded."""
    reads: set[str] = set()
    for py in root.rglob("*.py"):
        if py.name in _PLUMBING:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                reads.add(node.attr)
    return reads


def check(schema_path: Path, root: Path) -> list[str]:
    """THE DEFECT, second half: `field_name in reads` -- a bare NAME test, not
    `(owning_class, field_name)`."""
    fields = _collect_fields(schema_path)
    if not fields:
        return []
    source_lines = schema_path.read_text(encoding="utf-8").splitlines()
    reads = _collect_reads(root)
    errors: list[tuple[int, str]] = []
    for cls_name, field_name, lineno in fields:
        found, pointer = _check_marker(source_lines, lineno)
        if found:
            if not pointer:
                errors.append((lineno, f"{cls_name}.{field_name}: bare '# read-via:' marker"))
        elif field_name not in reads:
            errors.append((lineno, f"{cls_name}.{field_name} has no attribute-read site"))
    errors.sort(key=lambda t: t[0])
    return [msg for _, msg in errors]
