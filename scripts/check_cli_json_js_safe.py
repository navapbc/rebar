#!/usr/bin/env python3
"""CLI ``--output json`` JS-safe-integer gate (bug unhelping-creviced-rhino, e127-a3ad-895a-4a2f).

rebar stamps NANOSECOND timestamps (``created_at`` / ``updated_at`` / ``signed_at`` /
``timestamp``) as 19-digit ``time.time_ns()`` integers, far outside the RFC 8259 §6
interoperable range (``|n| > 2**53-1``). Emitted to a CLI ``--output json`` stream as a BARE
JSON number, every float64 consumer (``jq`` / Node ``JSON.parse`` / Ruby) SILENTLY rounds them
(``…898642`` -> ``…898600``, a -42 ns drift) and a lossless BigInt consumer (GitHub Copilot
CLI) dies re-stringifying with ``TypeError: Do not know how to serialize a BigInt``.

The fix routes CLI store-data serialization through the SINGLE choke point
:func:`rebar._mcp_errors.js_safe_dumps`, which runs ``js_safe_result`` (out-of-range int ->
exact decimal STRING) before ``json.dumps``. This gate keeps that choke point intact: it FAILS
if store data is written to stdout through a RAW ``json.dumps`` on the CLI surface, so a future
emitter cannot silently reintroduce a bare big-int on the wire.

WHAT IS FLAGGED — only the CONSTRUCT, never prose. A call whose result is written to stdout —
``print(<X>)``, ``sys.stdout.write(<X>)`` or ``stdout.write(<X>)`` — where ``<X>`` contains
(directly, or nested through ``+ "\n"`` / an f-string / any expression) a call to
``json.dumps`` / ``_json.dumps`` (attribute ``dumps`` on a ``json`` / ``_json`` module name).
The sanctioned choke point ``js_safe_dumps(...)`` is a plain ``Name`` callee and is NOT flagged.
Docstrings, comments and error text compose no stdout write and are never flagged.

SCAN ROOTS — the user-facing CLI ``--output json`` surface only: ``src/rebar/_cli``,
``src/rebar/_commands``, ``src/rebar/_engine_support`` and ``src/rebar/signing.py``. This DOES
cover the CLI LLM/eval verbs that live under ``src/rebar/_cli`` (e.g. ``_llm_eval_commands.py``),
which share the ``--output json`` wire contract. The structured-logging stream
(``rebar._logging``) and the reconciler daemon (``src/rebar/_engine/rebar_reconciler``) are
DIFFERENT contracts, live outside these roots, and are out of scope.

SANCTION — ``# js-safe-ok: <reason>``, with a MANDATORY reason, honoured on the offending line,
the line above, or the enclosing statement's first line (mirrors ``# repo-root-ok:`` /
``# tickets-boundary-ok:``). Use it for a stdout write that provably carries NO store
timestamps (a fixed diagnostic string, a list of backend names, an f-string of terminal
output). A bare ``# js-safe-ok`` with no reason is itself reported.

KNOWN LIMITATION — this is a SYNTACTIC gate: it flags a ``json.dumps`` only where it is
lexically nested inside a stdout-sink call's arguments. A value laundered through a HELPER —
``s = json.dumps(doc)`` on one statement, ``print(s)`` on another, or ``def f(): return
json.dumps(doc)`` written to stdout by a distant caller — is NOT caught, because tracking it
would need whole-program dataflow. The direct ``print(json.dumps(...))`` /
``sys.stdout.write(json.dumps(...) + "\n")`` shape is what every current CLI emitter uses and
what regressed here, so the gate pins that shape; the choke point plus the RED regression test
(``int(wire) == stored`` AND the wire is a JSON string) remain the primary defence, with this
gate as the belt-and-braces guard against the direct construct.
"""

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

#: ``file=`` targets that keep a ``print`` on the stdout stream. Anything else (``sys.stderr``,
#: a log handle) redirects it off stdout, so the JS-safe contract does not apply.
_STDOUT_FILE_TARGETS = {"sys.stdout", "stdout"}

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
    """Return the first ``json.dumps``/``_json.dumps`` Call anywhere under ``node``, else None.

    Walks the whole subtree so a dumps nested through ``+ "\n"``, an f-string, or any wrapping
    expression is still found. ``js_safe_dumps(...)`` (a bare ``Name`` callee) never matches.
    """
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
        """A ``print(..., file=X)`` only lands on stdout when ``X`` is stdout (or absent).

        ``print`` defaults to stdout, so a bare ``print(...)`` qualifies. An explicit
        ``file=sys.stderr`` (or any non-stdout handle) redirects it off the JS-safe wire — the
        docstring names stderr diagnostics as allowed — so it must NOT be flagged. The
        ``*.write`` sinks are already bound to a concrete stream by their receiver.
        """
        for keyword in node.keywords:
            if keyword.arg == "file":
                return _callee_name(keyword.value) in _STDOUT_FILE_TARGETS
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
    """Return (violations, bare_marker_findings) for one source file.

    A file that fails to parse is reported as a violation rather than silently skipped: an
    unparseable CLI module could otherwise hide a fresh ``print(json.dumps(...))`` from the
    gate. ``json.dumps`` absent means the file cannot contain the construct, so parsing it at
    all is unnecessary.
    """
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
