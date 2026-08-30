#!/usr/bin/env python3
"""Repo-root-from-package-location gate (bug ``impressive-doddering-alpinegoat``, c0b9).

Sibling of ``check_repo_root_from_tracker.py``. That gate drains one spelling of the
repo-root-resolution defect CLASS — deriving the code root from the STORE path
(``dirname(<tracker>)``). This gate drains the OTHER spelling — deriving the code root from
the PACKAGE LOCATION, ``Path(__file__).resolve().parents[N]``.

``Path(__file__).resolve().parents[N]`` is the repo root ONLY under an editable install, where
the package lives inside the checkout. Under a non-editable / wheel install the package lives
in ``site-packages``, so the same expression climbs to ``<venv>/lib/pythonX`` — a directory
with no ``.git`` and no ``src/rebar``. Code that then treats it as a checkout silently
reconciles the wrong tree (bug c0b9: the reconciler resolved
``repo_root = <venv>/lib/python3.13`` on a non-editable CI leg).

RESOLVE the code root through the ONE validated resolver, never re-derive it from the package
location:

    from rebar import config
    config.reconciler_repo_root()   # REBAR_ROOT > validated package root > cwd toplevel > error

WHAT IS FLAGGED — only the COMPOSING expression, never prose. The SUBSCRIPT root-climb
``<X>.parents[<n>]`` where ``<X>`` is ``Path(__file__)`` or ``Path(__file__).resolve()``
(``.absolute()`` too). The singular ``Path(__file__).parent`` package-relative idiom — 50+
legitimate sites locating package data / sibling modules — is a DIFFERENT construct (an
attribute access, not a ``.parents[...]`` subscript) and is NOT flagged. Docstrings and
comments compose nothing and never reach the AST, so they are never flagged.

SANCTION — ``# repo-root-ok: <reason>`` (mandatory reason), honoured on the offending line,
the line above, or the enclosing statement's first line. Used for a genuine package-location
derivation that is NOT a checkout root, e.g. ``__main__.py``'s ``parents[3]`` package parent
prepended to ``sys.path`` (install-location-relative by design, not a repo root).

SEAM — ``# repo-root-seam: <reason>`` exempts THE single validated resolver,
``config.reconciler_repo_root``, whose step-2 ``parents[2]`` candidate is used ONLY after
``_is_repo_checkout`` confirms it is a real checkout. Every reconciler surface routes repo-root
resolution through that resolver, so the raw construct exists in exactly one validated place.
A bare marker with no reason is itself reported.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# repo-root-ok: this gate's own script lives at <repo>/scripts/; not a runtime resolver
REPO_ROOT = Path(__file__).resolve().parents[1]

MARKER = "# repo-root-ok:"
BARE_MARKER = "# repo-root-ok"
SEAM_MARKER = "# repo-root-seam:"
#: Marker tokens (colon optional) whose ``:<reason>`` form sanctions a site and whose
#: reasonless form is itself reported. ``# repo-root-seam`` exempts the single validated
#: resolver; it is SEPARATE from ``# repo-root-ok`` so each carries its own intent.
_SANCTION_TOKENS = (BARE_MARKER, "# repo-root-seam")
SCAN_ROOT = "src"

#: one-arg callees that yield a ``pathlib.Path`` from ``__file__`` without changing which file
#: it names, so ``Path(__file__).resolve().parents[N]`` composes the same ancestor as
#: ``Path(__file__).parents[N]``.
_PATH_NORMALISERS = {"resolve", "absolute"}


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


def _is_path_of_file(node: ast.AST) -> bool:
    """True if ``node`` is ``Path(__file__)`` (optionally ``.resolve()`` / ``.absolute()``)."""
    if isinstance(node, ast.Call):
        callee = _callee_name(node.func)
        # ``Path(__file__)`` — bare or module-qualified (``pathlib.Path``).
        if callee == "Path" or callee.endswith(".Path"):
            return len(node.args) == 1 and _is_file_name(node.args[0])
        # ``<pathexpr>.resolve()`` / ``<pathexpr>.absolute()`` — a normaliser on a file path.
        if isinstance(node.func, ast.Attribute) and node.func.attr in _PATH_NORMALISERS:
            return _is_path_of_file(node.func.value)
    return False


def _is_file_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "__file__"


def _parents_climb_shape(node: ast.AST) -> str | None:
    """A human label if ``node`` is ``Path(__file__)[...].parents[<n>]``, else None."""
    if not isinstance(node, ast.Subscript):
        return None
    value = node.value
    if not (isinstance(value, ast.Attribute) and value.attr == "parents"):
        return None
    if not _is_path_of_file(value.value):
        return None
    return "`Path(__file__).parents[...]`"


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
        for token in _SANCTION_TOKENS:
            if token not in text:
                continue
            colon = f"{token}:"
            if colon in text and text.split(colon, 1)[1].strip():
                sanctioned = True
            else:
                bare = True
    return sanctioned, bare


class _Visitor(ast.NodeVisitor):
    """Collect ``Path(__file__)[...].parents[<n>]`` subscript root-climbs."""

    def __init__(self) -> None:
        self.hits: list[tuple[ast.AST, str]] = []

    def visit_Subscript(self, node: ast.Subscript) -> None:
        shape = _parents_climb_shape(node)
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
    """Return (violations, bare_marker_findings) for one source file.

    A file that fails to parse is reported as a violation rather than silently skipped: an
    unparseable production module could otherwise hide a fresh root-climb from the gate.
    ``.parents[`` absent means the file cannot contain the construct.
    """
    source = path.read_text(encoding="utf-8")
    rel = str(path.relative_to(root))
    if ".parents[" not in source:
        return [], []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [_Finding(rel, exc.lineno or 0, "unparseable source", str(exc))], []
    lines = source.splitlines()
    stmt_lines = _statement_lines(tree)
    visitor = _Visitor()
    visitor.visit(tree)

    violations: list[_Finding] = []
    bare_findings: list[_Finding] = []
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
        if finding.shape == "unparseable source":
            print(
                f"{finding.path}:{finding.line}: could not be parsed, so a "
                f"Path(__file__).parents[N] root-climb here would be missed: {finding.text}",
                file=sys.stderr,
            )
            continue
        print(
            f"{finding.path}:{finding.line}: derives the repo root from the package location "
            f"({finding.shape}): {finding.text}",
            file=sys.stderr,
        )
    total = len(violations) + len(bare_findings)
    print(
        f"\ncheck_repo_root_from_package: {total} site(s) deriving the repo root from the "
        f"package location.\n``Path(__file__).parents[N]`` is the checkout ONLY under an "
        f"editable install; under a wheel install it climbs into site-packages. RESOLVE the "
        f"root through the validated resolver instead:\n"
        f"    from rebar import config\n"
        f"    config.reconciler_repo_root()   # REBAR_ROOT > validated package root > "
        f"validated cwd toplevel > clear error\n"
        f"If the path genuinely is NOT a checkout root (e.g. a package parent for sys.path),\n"
        f"sanction it WITH A REASON:\n"
        f"    {MARKER} <why this package-location derivation is not a checkout root>",
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
