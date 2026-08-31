#!/usr/bin/env python3
"""POSIX-only-import collection guard (bug ``infamous-protected-baboon``, 0b31-aeb5-e734-41c9).

``fcntl`` is a POSIX-only stdlib module: it has no Windows build. An *unconditional*
module-level ``import fcntl`` therefore makes the whole module unimportable off POSIX, and
because ``import rebar`` transitively reaches such a module (``rebar._commands.doctor_locks``),
a single unguarded import turned the entire package unimportable on Windows — every test
module failed to *collect* with ``ModuleNotFoundError: No module named 'fcntl'`` (the
non-blocking "Test Suite (mirror)" Windows sweep tier went fully red).

The collectability contract is: a POSIX-only import that runs at module import time MUST be
guarded, so the module still imports where the dependency is absent::

    try:  # POSIX advisory locking; absent on some platforms (e.g. plain Windows)
        import fcntl
    except ImportError:  # pragma: no cover - platform-dependent
        fcntl = None  # type: ignore[assignment]

(actually *using* ``fcntl`` off POSIX is out of scope — Windows is not a declared support
target — only that import/collection succeeds.)

This gate is a deterministic AST check, not an LLM criterion: the property is exactly
"a module-scope ``import fcntl`` that is not lexically inside a ``try``". It is stdlib-only
and needs no CI provider (project.portability) — it runs the same locally and in any CI::

    python scripts/check_fcntl_import_guard.py
    make lint                                   # wired in

What is FLAGGED — an *unconditional* module-scope ``import fcntl`` / ``from fcntl import ...``
(one not made conditional by a ``try`` or an ``if``). What is NOT flagged:

* an import inside a **function/method body** — that is lazy and only runs when called, so it
  never breaks *collection* (e.g. ``rebar._store.hlc`` imports fcntl lazily);
* an import inside a **``try``** block — the ``try: import fcntl / except ImportError``
  idiom this gate exists to require;
* an import inside an **``if``/``else``** branch — a platform guard such as
  ``if sys.platform != "win32": import fcntl / else: fcntl = None`` is equally
  collection-safe (see ``rebar._engine.rebar_reconciler.alert_store``);
* a line carrying the sanction marker ``# fcntl-guard-ok: <reason>`` (reason MANDATORY) — for
  a genuinely collection-safe module-scope import the AST cannot otherwise prove safe.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "rebar"

# POSIX-only stdlib modules that must be guarded when imported at module scope. Scoped to the
# confirmed offender for this class; extendable (e.g. termios/pwd/grp) as new members surface.
POSIX_ONLY_MODULES = frozenset({"fcntl"})

MARKER = "# fcntl-guard-ok:"


class _Finding:
    __slots__ = ("lineno", "module", "path")

    def __init__(self, path: Path, lineno: int, module: str) -> None:
        self.path = path
        self.lineno = lineno
        self.module = module


def _targets_posix_only(node: ast.stmt) -> str | None:
    """Return the POSIX-only module name this import statement targets, else ``None``."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in POSIX_ONLY_MODULES:
                return alias.name
    elif isinstance(node, ast.ImportFrom):
        if node.module in POSIX_ONLY_MODULES:
            return node.module
    return None


def _scan(
    body: list[ast.stmt], *, guarded: bool, lines: list[str], findings: list[_Finding], path: Path
) -> None:
    """Walk *body* recursively, flagging unconditional module-scope POSIX-only imports.

    ``guarded`` is True inside a construct that makes the import *conditional* — a ``try``
    block (``try: import fcntl / except ImportError: fcntl = None``) or an ``if``/``else``
    branch (``if sys.platform != "win32": import fcntl / else: fcntl = None``). Both are
    established, collection-safe idioms in this codebase. Function/async-function bodies are
    skipped entirely: an import there is lazy and never runs at collection time.
    """
    for node in body:
        module = _targets_posix_only(node)
        if module is not None:
            if not guarded and not _is_sanctioned(lines, node.lineno):
                findings.append(_Finding(path, node.lineno, module))
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # lazy: collection-safe
        if isinstance(node, ast.Try):
            _scan(node.body, guarded=True, lines=lines, findings=findings, path=path)
            for handler in node.handlers:
                _scan(handler.body, guarded=True, lines=lines, findings=findings, path=path)
            _scan(node.orelse, guarded=True, lines=lines, findings=findings, path=path)
            _scan(node.finalbody, guarded=guarded, lines=lines, findings=findings, path=path)
            continue
        if isinstance(node, ast.If):
            # A module-scope ``if`` makes the import conditional (typically a platform
            # guard), so imports in either branch are collection-safe.
            _scan(node.body, guarded=True, lines=lines, findings=findings, path=path)
            _scan(node.orelse, guarded=True, lines=lines, findings=findings, path=path)
            continue
        # Other compound statements (with/class/for/while) execute unconditionally, so they
        # preserve — never confer — guarded status.
        for child_body in _child_bodies(node):
            _scan(child_body, guarded=guarded, lines=lines, findings=findings, path=path)


def _child_bodies(node: ast.stmt) -> list[list[ast.stmt]]:
    bodies: list[list[ast.stmt]] = []
    for attr in ("body", "orelse", "finalbody"):
        value = getattr(node, attr, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            bodies.append(value)
    return bodies


def _is_sanctioned(lines: list[str], lineno: int) -> bool:
    if 1 <= lineno <= len(lines):
        return MARKER in lines[lineno - 1]
    return False


def _scan_file(path: Path) -> list[_Finding]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:  # pragma: no cover - a syntax error is another gate's job
        return []
    lines = text.splitlines()
    findings: list[_Finding] = []
    _scan(tree.body, guarded=False, lines=lines, findings=findings, path=path)
    return findings


def find_violations(root: Path) -> list[_Finding]:
    findings: list[_Finding] = []
    for path in sorted(root.rglob("*.py")):
        findings.extend(_scan_file(path))
    return findings


def _report(findings: list[_Finding]) -> None:
    print(
        f"\ncheck_fcntl_import_guard: {len(findings)} unguarded module-scope POSIX-only "
        f"import(s).\nA module-scope ``import fcntl`` runs at collection time and has no "
        f"Windows build, so it makes the module (and any importer, including ``import "
        f"rebar``) fail to collect off POSIX. GUARD it:\n"
        f"    try:  # POSIX advisory locking; absent on some platforms\n"
        f"        import fcntl\n"
        f"    except ImportError:  # pragma: no cover - platform-dependent\n"
        f"        fcntl = None  # type: ignore[assignment]\n"
        f"Move a genuinely lazy use into the function body instead, or — if the import is "
        f"provably collection-safe — sanction it WITH A REASON:\n"
        f"    {MARKER} <why this module-scope import is collection-safe>",
        file=sys.stderr,
    )
    for finding in findings:
        rel = finding.path.relative_to(REPO_ROOT)
        print(
            f"  {rel}:{finding.lineno}: unguarded module-scope import {finding.module}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=SRC_ROOT, help="source root to scan (default: src/rebar)"
    )
    args = parser.parse_args(argv)
    findings = find_violations(args.root)
    if findings:
        _report(findings)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
