#!/usr/bin/env python3
"""Repo/config-root-from-tracker gate (bug auspicial-friended-merganser, 2ec7-be89-9b01-496a).

The ticket store is RELOCATABLE: ``REBAR_TRACKER_DIR`` (and the ``tracker.dir`` config key)
can move it OUT of the checkout entirely — the deployed MCP server runs exactly that topology
(store at ``/var/gerrit/site/mcp-tickets``, code at ``/app``). Code that derives the
repo/config root as ``os.path.dirname(tracker)`` is therefore correct ONLY for a co-located
store: on a relocated one the tracker's parent is a directory with no ``rebar.toml``, so every
config read there silently resolves an EMPTY config. That is how the ``transition
open -> in_progress`` plan-review start-work gate came to read its flag as OFF and stop
enforcing on the deployed server, and how clarity/compaction config was silently ignored.

The store is RELOCATABLE — RESOLVE the code root, never compose it from the store path:

    from rebar import config
    config.repo_root(repo_root)       # explicit repo_root > REBAR_ROOT > git toplevel of cwd
    config.repo_root_or_none()        # same, but None instead of raising when cwd is gone

or thread the in-scope ``repo_root`` parameter (``None`` == discover) down to the config read.

WHAT IS FLAGGED — only the COMPOSING expression, never prose. A call of the shape
``os.path.dirname(<store>)`` (also ``dirname(...)`` / ``_os.path.dirname(...)``) where
``<store>`` is:

  1. the name ``tracker``                       ``os.path.dirname(tracker)``
  2. a ``*.tracker_dir(...)`` resolver call      ``os.path.dirname(reads.tracker_dir())``

Docstrings, comments, and error text are NOT flagged — they compose nothing, and comments never
reach the AST at all.

SANCTION — ``# repo-root-ok: <reason>``, with a MANDATORY reason, honoured on the offending
line, the line above, or the enclosing statement's first line (mirrors
``# tickets-boundary-ok:`` / ``# raw-git-ok:``). Exactly one site is sanctioned today: the
detached ``run_sweep`` child in ``compact_trigger.py``, whose cwd IS the store and which takes
only ``tracker`` — giving it a code root needs a spawn-contract change, tracked as an
``auspicial-friended-merganser`` follow-up. A bare marker with no reason is itself reported.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MARKER = "# repo-root-ok:"
BARE_MARKER = "# repo-root-ok"
SCAN_ROOT = "src"

#: dirname callees that compose a parent path. ``_os`` is an occasional local alias.
_DIRNAME_CALLEES = {"os.path.dirname", "dirname", "_os.path.dirname"}


class _Finding:
    __slots__ = ("line", "path", "shape", "text")

    def __init__(self, path: str, line: int, shape: str, text: str) -> None:
        self.path = path
        self.line = line
        self.shape = shape
        self.text = text


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        prefix = _callee_name(func.value)
        return f"{prefix}.{func.attr}" if prefix else func.attr
    return ""


def _store_shape(arg: ast.AST) -> str | None:
    """Return a human label if ``arg`` names the STORE (a tracker), else None."""
    if isinstance(arg, ast.Name) and arg.id == "tracker":
        return "`tracker`"
    if isinstance(arg, ast.Call):
        callee = _callee_name(arg.func)
        if callee == "tracker_dir" or callee.endswith(".tracker_dir"):
            return f"`{callee}(...)`"
    return None


def _marked(lines: list[str], line_no: int, stmt_line: int | None) -> tuple[bool, bool]:
    """(sanctioned, bare_marker_seen) for a finding at ``line_no`` (1-based)."""
    candidates = {line_no, line_no - 1}
    if stmt_line is not None:
        candidates.add(stmt_line)
    sanctioned = False
    bare = False
    for candidate in candidates:
        if not 1 <= candidate <= len(lines):
            continue
        text = lines[candidate - 1]
        if MARKER in text and text.split(MARKER, 1)[1].strip():
            sanctioned = True
        elif BARE_MARKER in text:
            bare = True
    return sanctioned, bare


class _Visitor(ast.NodeVisitor):
    """Collect ``dirname(<store>)`` compositions, with the store shape that matched."""

    def __init__(self) -> None:
        self.hits: list[tuple[ast.AST, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        if _callee_name(node.func) in _DIRNAME_CALLEES and len(node.args) == 1:
            shape = _store_shape(node.args[0])
            if shape is not None:
                self.hits.append((node, shape))
        self.generic_visit(node)


def _statement_lines(tree: ast.AST) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and hasattr(node, "lineno"):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            for line in range(node.lineno, end + 1):
                mapping.setdefault(line, node.lineno)
    return mapping


def scan_file(path: Path, root: Path) -> tuple[list[_Finding], list[_Finding]]:
    """Return (violations, bare_marker_findings) for one source file."""
    source = path.read_text(encoding="utf-8")
    if "dirname(" not in source:
        return [], []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], []
    lines = source.splitlines()
    stmt_lines = _statement_lines(tree)
    visitor = _Visitor()
    visitor.visit(tree)

    violations: list[_Finding] = []
    bare_findings: list[_Finding] = []
    rel = str(path.relative_to(root))
    for node, shape in visitor.hits:
        line_no = getattr(node, "lineno", 0)
        sanctioned, bare = _marked(lines, line_no, stmt_lines.get(line_no))
        if sanctioned:
            continue
        text = lines[line_no - 1].strip() if 1 <= line_no <= len(lines) else ""
        finding = _Finding(rel, line_no, shape, text)
        (bare_findings if bare else violations).append(finding)
    return violations, bare_findings


def find_violations(root: Path) -> tuple[list[_Finding], list[_Finding]]:
    violations: list[_Finding] = []
    bare_findings: list[_Finding] = []
    for path in sorted((root / SCAN_ROOT).rglob("*.py")):
        file_violations, file_bare = scan_file(path, root)
        violations.extend(file_violations)
        bare_findings.extend(file_bare)
    return violations, bare_findings


def _report(violations: list[_Finding], bare_findings: list[_Finding]) -> None:
    for finding in bare_findings:
        print(
            f"{finding.path}:{finding.line}: repo-root marker has NO REASON "
            f"({finding.shape}): {finding.text}",
            file=sys.stderr,
        )
    for finding in violations:
        print(
            f"{finding.path}:{finding.line}: derives the repo/config root from the store "
            f"({finding.shape}): {finding.text}",
            file=sys.stderr,
        )
    total = len(violations) + len(bare_findings)
    print(
        f"\ncheck_repo_root_from_tracker: {total} site(s) deriving the code root from the "
        f"store path.\nThe store is RELOCATABLE (REBAR_TRACKER_DIR) — RESOLVE the code root "
        f"instead of composing it:\n"
        f"    from rebar import config\n"
        f"    config.repo_root(repo_root)      # explicit > REBAR_ROOT > git toplevel of cwd\n"
        f"    config.repo_root_or_none()       # None instead of raising when cwd is gone\n"
        f"  or thread the in-scope `repo_root` (None == discover) to the config read.\n"
        f"If the path genuinely is NOT a code root (a detached child whose cwd is the store),\n"
        f"sanction it WITH A REASON:\n"
        f"    {MARKER} <why the store's parent is acceptable here>",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root to scan (default: repo)"
    )
    args = parser.parse_args(argv)
    violations, bare_findings = find_violations(args.root)
    if violations or bare_findings:
        _report(violations, bare_findings)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
