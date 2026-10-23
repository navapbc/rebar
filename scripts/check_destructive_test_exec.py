#!/usr/bin/env python3
"""Reject tests that execute shell deletion with an unguarded variable target.

An injectable deletion command (such as ``${RM_CMD:-rm}``) lets tests record
argv without deleting. A preceding ``: "${dir:?...}"`` guard also accepts a
targeted variable by rejecting unset and empty values; ``set -u`` alone does
not protect an empty value. This static check covers reviewable shell calls;
the OS sandbox remains the runtime boundary. See ticket 6818-615f-555e-4bb9."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

REPO_ROOT = Path(__file__).resolve().parents[1]

_EXEC_FUNCS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

#: Match rm flags as a set, covering every ordering and case of recursive
#: force without an incomplete alternation.
_RM_RE = re.compile(r"\brm\b((?:\s+-[A-Za-z]+)+)\s+([^\n;&|]*)")


def _is_destructive(flags: str) -> bool:
    """True when the flag cluster carries BOTH recursive and force, in any spelling."""
    letters = set(flags.replace("-", "").replace(" ", ""))
    return bool(letters & {"r", "R"}) and "f" in letters


#: A ``$VAR`` / ``${VAR}`` interpolation inside a deletion target.
_INTERP_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")

#: The injectable-seam escape: deletion routed through an overridable command.
_SEAM_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*_CMD:-\s*rm\s*\}")

_TEACHING = (
    'Route the deletion through a seam the test can stub — `"${RM_CMD:-rm}"` — and\n'
    'assert on the recorded argv, or guard the target with `: "${dir:?}"` plus an\n'
    "explicit deny-list. `set -u` does NOT cover this: it fires on unset, not on\n"
    'set-but-empty, and an empty path is what expands `"$dir"/*` to `/*`.'
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    detail: str


def _guarded_before(lines: Sequence[str], upto: int) -> set[str]:
    """Variables carrying a ``${var:?}`` guard STRICTLY BEFORE line ``upto``.

    Ordering matters: a guard written after the deletion runs after it, so a
    position-blind scan would clear a deletion the guard never actually protected.
    """
    joined = "\n".join(lines[: upto - 1])
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?", joined))


def unguarded_deletions(script_text: str) -> list[tuple[int, str]]:
    """Return (line_no, target) for each destructive deletion lacking a guard."""
    lines = script_text.splitlines()
    out: list[tuple[int, str]] = []
    for line_no, raw in enumerate(lines, start=1):
        if raw.lstrip().startswith("#"):
            continue
        # Drop a trailing comment so a seam or guard mentioned only in prose cannot
        # clear a live deletion on the same line.
        line = raw.split(" #", 1)[0]
        guarded = _guarded_before(lines, line_no)
        for match in _RM_RE.finditer(line):
            if not _is_destructive(match.group(1)):
                continue
            target = match.group(2).strip()
            if _SEAM_RE.search(line):
                continue
            names = _INTERP_RE.findall(target)
            if not names:
                continue  # a literal or relative-glob target carries no expansion risk
            if all(n in guarded for n in names):
                continue
            out.append((line_no, target))
    return out


def _script_literals(call: ast.Call) -> list[str]:
    """String constants in the call that name a shell script."""
    found: list[str] = []
    for sub in ast.walk(call):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if sub.value.endswith(".sh"):
                found.append(sub.value)
    return found


def _is_exec_call(node: ast.AST) -> TypeGuard[ast.Call]:
    """Detect a test call naming a shell script even through subprocess aliases or wrappers;
    static analysis cannot resolve arbitrary wrapper behavior."""
    return isinstance(node, ast.Call) and bool(_script_literals(node))


def scan_file(path: Path, rel: Path, root: Path) -> list[Finding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not _is_exec_call(node):
            continue
        for literal in _script_literals(node):
            script = (root / literal.lstrip("./")).resolve()
            if not script.is_file():
                continue
            for script_line, target in unguarded_deletions(script.read_text(encoding="utf-8")):
                findings.append(
                    Finding(
                        str(rel),
                        node.lineno,
                        "destructive-test-exec",
                        f"execs {literal} which deletes unguarded target {target!r} "
                        f"at {literal}:{script_line}",
                    )
                )
    return findings


def find_violations(root: Path) -> list[Finding]:
    tests = root / "tests"
    if not tests.is_dir():
        return []
    findings: list[Finding] = []
    for path in sorted(tests.rglob("*.py")):
        findings.extend(scan_file(path, path.relative_to(root), root))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to scan")
    args = parser.parse_args(argv)

    violations = find_violations(Path(args.root))
    if violations:
        print(
            f"destructive-test-exec: {len(violations)} unguarded exec(s) found\n",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v.path}:{v.line}  [{v.kind}]  {v.detail}", file=sys.stderr)
        print(f"\n{_TEACHING}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
