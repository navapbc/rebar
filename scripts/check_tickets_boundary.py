#!/usr/bin/env python3
"""Reject store paths that bypass the relocatable tracker configuration.

``rebar.config.tracker_dir()`` honors ``REBAR_TRACKER_DIR`` and ``tracker.dir``. The AST gate
therefore flags a ``.tickets-tracker`` literal used in:

1. a ``/`` path join;
2. an ``os.path.join`` argument;
3. a ``Path(...)`` argument; or
4. a constant bound to the bare directory name.

Docstrings, comments, errors, and help text are excluded because they do not compose paths.
Sanction a legitimate layout use with ``# tickets-boundary-ok: <reason>`` on the expression,
preceding line, or enclosing definition/assignment. Empty reasons fail. Legitimate examples
include the resolver's own default and paths inside a newly created temporary snapshot.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The literal whose composition this gate governs. Read from the schema default rather than
#: duplicated as a third copy of the string.
TRACKER_LITERAL = ".tickets-tracker"

MARKER = "# tickets-boundary-ok:"
#: The pre-existing reasonless form. Detected separately so its diagnostic can say what to do
#: rather than reporting the line as unmarked, which would read as a false negative.
BARE_MARKER = "# tickets-boundary-ok"

SCAN_ROOT = "src"


class _Finding:
    __slots__ = ("line", "path", "shape", "text")

    def __init__(self, path: str, line: int, shape: str, text: str) -> None:
        self.path = path
        self.line = line
        self.shape = shape
        self.text = text


def _is_tracker_literal(node: ast.AST) -> bool:
    """True for a string constant that NAMES the store dir (not one that merely mentions it).

    ``".tickets-tracker"`` and ``".tickets-tracker/.bridge_state/x.json"`` name it; a sentence
    such as ``"... commit .tickets-tracker and ensure ..."`` mentions it. The distinction is
    the whole reason prose does not have to be marked.
    """
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    value = node.value
    return value == TRACKER_LITERAL or value.startswith(TRACKER_LITERAL + "/")


def _marked(lines: list[str], line_no: int, stmt_line: int | None) -> tuple[bool, bool]:
    """(sanctioned, bare_marker_seen) for a finding at ``line_no`` (1-based).

    Checked on the offending line, the line above, and the enclosing statement's first line.
    """
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
    """Collect composing uses of the tracker literal, with the shape that made each compose."""

    def __init__(self) -> None:
        self.hits: list[tuple[ast.AST, str]] = []

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div) and (
            _is_tracker_literal(node.left) or _is_tracker_literal(node.right)
        ):
            operand = node.left if _is_tracker_literal(node.left) else node.right
            self.hits.append((operand, "path join with `/`"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee_name(node.func)
        if name in {"join", "os.path.join", "Path", "PurePath", "PosixPath"}:
            for arg in node.args:
                if _is_tracker_literal(arg):
                    self.hits.append((arg, f"`{name}(...)` argument"))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_tracker_literal(node.value):
            targets = ", ".join(t.id for t in node.targets if isinstance(t, ast.Name)) or "<target>"
            self.hits.append((node.value, f"dir-name constant `{targets}`"))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and _is_tracker_literal(node.value):
            target = node.target.id if isinstance(node.target, ast.Name) else "<target>"
            self.hits.append((node.value, f"dir-name constant `{target}`"))
        self.generic_visit(node)


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        prefix = _callee_name(func.value)
        return f"{prefix}.{func.attr}" if prefix else func.attr
    return ""


def _statement_lines(tree: ast.AST) -> dict[int, int]:
    """Map every line inside a statement to that statement's first line."""
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
    if TRACKER_LITERAL not in source:
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
            f"{finding.path}:{finding.line}: tickets-boundary marker has NO REASON "
            f"({finding.shape}): {finding.text}",
            file=sys.stderr,
        )
    for finding in violations:
        print(
            f"{finding.path}:{finding.line}: composes a store path from the hardcoded "
            f"{TRACKER_LITERAL!r} ({finding.shape}): {finding.text}",
            file=sys.stderr,
        )
    total = len(violations) + len(bare_findings)
    print(
        f"\ncheck_tickets_boundary: {total} unsanctioned store-path composition(s).\n"
        f"The store is RELOCATABLE — resolve it instead of composing it:\n"
        f"    from rebar import config\n"
        f"    config.tracker_dir(repo_root)          # -> Path\n"
        f"  reconciler code may use the already-resolved `settings.tracker_dir`.\n"
        f"If the path genuinely is NOT the configured store (a temp dir this code just\n"
        f"created, or the default name inside a resolver), sanction it WITH A REASON:\n"
        f"    {MARKER} <why relocation cannot affect this path>",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to scan")
    args = parser.parse_args(argv)

    violations, bare_findings = find_violations(Path(args.root))
    if violations or bare_findings:
        _report(violations, bare_findings)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
