#!/usr/bin/env python3
"""Guard CLI JSON stdout against raw unsafe integers (ticket e127-a3ad-895a-4a2f).

Nanosecond store timestamps exceed JavaScript's exact integer range. CLI store
data must use ``js_safe_dumps``, which encodes out-of-range integers as exact
decimal strings. Scan ``_cli``, ``_commands``, ``_engine_support``, and
``signing.py`` for ``json.dumps`` or ``_json.dumps`` nested anywhere inside
``print`` or stdout ``write`` expressions. Structured logging and reconciler
daemon output have separate contracts.

``# js-safe-ok: <reason>`` sanctions a proven safe stdout write on that line,
the prior line, or the statement's first line; a missing reason is an error.
This syntactic guard cannot track serialized values through helper calls or
separate statements. Wire round-trip tests cover the runtime contract."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MARKER = "# js-safe-ok:"
BARE_MARKER = "# js-safe-ok"

#: CLI ``--output json`` surface. Directories are scanned recursively; files are scanned as-is.
SCAN_ROOTS = (
    "src/rebar/_cli",
    "src/rebar/_commands",
    "src/rebar/_engine_support",
    "src/rebar/signing.py",
)

#: The stdout sinks a ``--output json`` value flows through.
_STDOUT_SINKS = {"print", "sys.stdout.write", "stdout.write"}

#: Only proven stderr file= targets are exempt. Ambiguous targets remain
#: stdout candidates unless sanctioned with # js-safe-ok: <reason>.
_NON_STDOUT_FILE_TARGETS = {"sys.stderr", "stderr"}

#: Raw dumps callees the choke point replaces. ``_json`` is the common local alias.
_RAW_DUMPS_CALLEES = {"json.dumps", "_json.dumps"}


class _Finding:
    __slots__ = ("line", "path", "text")

    def __init__(self, path: str, line: int, text: str) -> None:
        self.path = path
        self.line = line
        self.text = text


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        prefix = _callee_name(func.value)
        return f"{prefix}.{func.attr}" if prefix else func.attr
    return ""


def _contains_raw_dumps(node: ast.AST) -> ast.Call | None:
    """Find raw json.dumps or _json.dumps anywhere in an expression, including nested
    concatenation and f-strings; exclude js_safe_dumps."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _callee_name(child.func) in _RAW_DUMPS_CALLEES:
            return child
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
    """Collect stdout-sink writes whose value is built by a raw ``json.dumps``."""

    def __init__(self) -> None:
        self.hits: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        if _callee_name(node.func) in _STDOUT_SINKS and self._writes_to_stdout(node):
            for arg in node.args:
                raw = _contains_raw_dumps(arg)
                if raw is not None:
                    self.hits.append(raw)
                    break
        self.generic_visit(node)

    @staticmethod
    def _writes_to_stdout(node: ast.Call) -> bool:
        """Treat print as stdout unless its file target is provably stderr. Ambiguous targets fail
        closed; sanctioned exceptions need # js-safe-ok: <reason>."""
        for keyword in node.keywords:
            if keyword.arg == "file":
                return _callee_name(keyword.value) not in _NON_STDOUT_FILE_TARGETS
        return True


def _statement_lines(tree: ast.AST) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and hasattr(node, "lineno"):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            for line in range(node.lineno, end + 1):
                mapping.setdefault(line, node.lineno)
    return mapping


def scan_file(path: Path, root: Path) -> tuple[list[_Finding], list[_Finding]]:
    """Return violations and bare sanctions. Unparseable files fail closed; files without raw
    dumps need no AST scan."""
    source = path.read_text(encoding="utf-8")
    rel = str(path.relative_to(root))
    if "json.dumps" not in source:
        return [], []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [_Finding(rel, exc.lineno or 0, f"unparseable source: {exc}")], []
    lines = source.splitlines()
    stmt_lines = _statement_lines(tree)
    visitor = _Visitor()
    visitor.visit(tree)

    violations: list[_Finding] = []
    bare_findings: list[_Finding] = []
    for node in visitor.hits:
        line_no = getattr(node, "lineno", 0)
        sanctioned, bare = _marked(lines, line_no, stmt_lines.get(line_no))
        if sanctioned:
            continue
        text = lines[line_no - 1].strip() if 1 <= line_no <= len(lines) else ""
        finding = _Finding(rel, line_no, text)
        (bare_findings if bare else violations).append(finding)
    return violations, bare_findings


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for entry in SCAN_ROOTS:
        target = root / entry
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            files.append(target)
    return files


def find_violations(root: Path) -> tuple[list[_Finding], list[_Finding]]:
    violations: list[_Finding] = []
    bare_findings: list[_Finding] = []
    for path in _iter_files(root):
        file_violations, file_bare = scan_file(path, root)
        violations.extend(file_violations)
        bare_findings.extend(file_bare)
    return violations, bare_findings


def _report(violations: list[_Finding], bare_findings: list[_Finding]) -> None:
    for finding in bare_findings:
        print(
            f"{finding.path}:{finding.line}: js-safe marker has NO REASON: {finding.text}",
            file=sys.stderr,
        )
    for finding in violations:
        if finding.text.startswith("unparseable source"):
            print(
                f"{finding.path}:{finding.line}: could not be parsed, so a "
                f"print(json.dumps(...)) site here would be missed: {finding.text}",
                file=sys.stderr,
            )
            continue
        print(
            f"{finding.path}:{finding.line}: writes store data to stdout through a RAW "
            f"json.dumps: {finding.text}",
            file=sys.stderr,
        )
    total = len(violations) + len(bare_findings)
    print(
        f"\ncheck_cli_json_js_safe: {total} CLI --output json site(s) serializing through a "
        f"raw json.dumps.\nrebar's 19-digit nanosecond timestamps are OUTSIDE the RFC 8259 §6 "
        f"interoperable range, so a bare JSON number is silently rounded by float64 consumers "
        f"(jq/Node/Ruby) and breaks BigInt consumers (GitHub Copilot CLI). Route the emit "
        f"through the single choke point:\n"
        f"    from rebar._mcp_errors import js_safe_dumps\n"
        f"    print(js_safe_dumps(payload, indent=2, ensure_ascii=False))\n"
        f"If the write provably carries NO store timestamps (a fixed diagnostic string, a list "
        f"of names), sanction it WITH A REASON:\n"
        f"    {MARKER} <why this stdout write carries no ns timestamp>",
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
