#!/usr/bin/env python3
"""Destructive test-exec lint [rebar:6818-615f-555e-4bb9].

A test may not ``subprocess``-exec a shell script whose deletion target is an
unguarded variable interpolation. On 2026-08-26 a test did exactly that: it ran a
real script containing ``rm -rf "${dir}"/*`` with ``dir`` set to ``""``, the glob
expanded to ``rm -rf /*``, and the run destroyed ``/opt/homebrew`` and every
Homebrew-installed app in ``/Applications`` before a 60-second timeout stopped it.

The deeper defect is the test design, not the script: a test asserting *"the guard
rejects an unsafe path"* should never be able to delete anything. Asserting on **what
would have been deleted** — via an injectable seam — is a strictly better oracle than
observing a wiped directory, and it removes the hazard instead of containing it.

Two shapes clear this gate:

* a **seam** — the script deletes through ``"${RM_CMD:-rm}"``, so a test can point
  ``RM_CMD`` at a stub that records argv;
* a **shell-level guard** — ``: "${dir:?...}"`` for the same variable the deletion
  targets, which aborts on unset AND on set-but-empty.

Note that ``set -euo pipefail`` does NOT clear this gate, and deliberately so: ``set
-u`` fires on *unset*, not set-but-empty, and the incident's variable was ``""``.

This is static analysis over the reviewable surface. It cannot stop a mutation at
runtime — a mutation harness can delete any guard expressed in the artifact it
mutates — so it is defence in depth behind the OS sandbox tracked by
``e668-b496-e264-4283``, never a substitute for it.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

REPO_ROOT = Path(__file__).resolve().parents[1]

_EXEC_FUNCS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

#: ``rm`` carrying BOTH recursive and force, in either flag spelling/order.
_RM_RE = re.compile(
    r"\brm\s+(?:-[a-zA-Z]*(?:rf|fr)[a-zA-Z]*|(?:-[rR]\s+-f|-f\s+-[rR]))\S*\s+([^\n;&|]*)"
)

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


def _guarded_vars(script_text: str) -> set[str]:
    """Variables carrying a ``${var:?}`` abort guard anywhere in the script."""
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?", script_text))


def unguarded_deletions(script_text: str) -> list[tuple[int, str]]:
    """Return (line_no, target) for each destructive deletion lacking a guard."""
    guarded = _guarded_vars(script_text)
    out: list[tuple[int, str]] = []
    for line_no, line in enumerate(script_text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for match in _RM_RE.finditer(line):
            target = match.group(1).strip()
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
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _EXEC_FUNCS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    )


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
