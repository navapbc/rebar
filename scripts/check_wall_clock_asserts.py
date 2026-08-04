#!/usr/bin/env python3
"""Wall-clock upper-bound assert lint over ``tests/**``.

Policy [rebar:1e95-fc5c-bca8-44c7]: an UPPER-BOUND wall-clock assertion in a
test (``assert elapsed < N`` and kin) is the proven CI flake class under
runner contention — bugs 19d7, 5e94, edfe, 85c3 all trace to one. A tight
budget that passes on a quiet laptop flakes on a loaded runner; the fix is a
counting/structural proxy on the code path (spy the miss-path function), or
an honest hang-guard with a generous ceiling.

Detection (AST, per test file): an ``assert`` whose comparison is ``<`` /
``<=`` against a numeric budget, where the measured side references a
wall-clock quantity — a name containing ``elapsed``/``duration``/``took``,
or an inline subtraction of ``time.monotonic()`` / ``time.time()`` /
``time.perf_counter()`` readings. Lower-bound asserts (``>`` / ``>=``) are
out of scope: they cannot flake from a SLOW runner.

Two escapes, and only these:

- ``# timing: hang-guard — <reason>`` on the assert's lines or the line
  directly above (the reason is MANDATORY — an empty reason still fires):
  sanctions a deliberate stuck-run guard whose ceiling dwarfs the expected
  wall time.
- the perf-lane CI-exclusion guard on the enclosing test —
  ``@pytest.mark.skipif(os.environ.get("CI") == "true", ...)`` (the idiom
  the reducer benchmarks use). The bare ``@pytest.mark.benchmark`` marker is
  NOT an escape: it is registered but no CI invocation filters it out, so a
  benchmark-marked test still runs on CI.

The lint's own unit-test file carries live upper-bound asserts as fixtures,
so it is structurally excluded (EXCLUDED_FILES — the check_comment_hygiene.py
idiom, recorded after b047's guard self-tripped on its fixtures).
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

EXCLUDED_FILES = (Path("tests/unit/test_wall_clock_assert_lint.py"),)

_TIMING_NAME = re.compile(r"elapsed|duration|took", re.IGNORECASE)
_CLOCK_CALLS = {"monotonic", "time", "perf_counter", "monotonic_ns", "perf_counter_ns"}
_MARKER = re.compile(r"#\s*timing:\s*hang-guard\s*(?P<reason>.*)$")

_TEACHING = """\
Upper-bound wall-clock asserts are the proven CI flake class under runner
contention (bugs 19d7, 5e94, edfe, 85c3): a budget that passes on a quiet
laptop flakes on a loaded runner. Prefer a counting/structural proxy on the
code path (spy the miss-path function the fast path must never call). If the
assert is genuinely a stuck-run guard, sanction it inline with a reason:
    # timing: hang-guard — <why the ceiling dwarfs the expected wall time>
or move the test to the perf lane with the CI-exclusion guard:
    @pytest.mark.skipif(os.environ.get("CI") == "true", reason="...")
(the bare @pytest.mark.benchmark marker is NOT an escape — CI still runs it)."""


@dataclass
class Finding:
    path: Path
    line: int
    text: str
    why: str


def _has_timing_operand(node: ast.expr) -> bool:
    """The measured side references a wall-clock quantity."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and _TIMING_NAME.search(sub.id):
            return True
        if isinstance(sub, ast.Attribute) and _TIMING_NAME.search(sub.attr):
            return True
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Sub):
            for side in (sub.left, sub.right):
                if isinstance(side, ast.Call):
                    fn = side.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else (
                        fn.id if isinstance(fn, ast.Name) else ""
                    )
                    if name in _CLOCK_CALLS:
                        return True
    return False


def _is_numeric_budget(node: ast.expr) -> bool:
    """The bound side is a literal numeric budget (possibly simple arithmetic)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, (int, float)):
            return True
    return False


def _upper_bound_wall_clock(test: ast.expr) -> bool:
    for sub in ast.walk(test):
        if not isinstance(sub, ast.Compare):
            continue
        left = sub.left
        for op, comparator in zip(sub.ops, sub.comparators):
            if isinstance(op, (ast.Lt, ast.LtE)):
                if _has_timing_operand(left) and _is_numeric_budget(comparator):
                    return True
            elif isinstance(op, (ast.Gt, ast.GtE)):
                # lower bound written measured-side-right: N > elapsed IS an
                # upper bound on elapsed.
                if _has_timing_operand(comparator) and _is_numeric_budget(left):
                    return True
            left = comparator
    return False


def _marker_reason(lines: list[str], start: int, end: int) -> str | None:
    """Return the marker reason if a hang-guard marker covers lines start..end
    (1-based, inclusive) or the line directly above; None when unmarked."""
    lo = max(0, start - 2)
    for idx in range(lo, min(end, len(lines))):
        m = _MARKER.search(lines[idx])
        if m:
            return m.group("reason").strip(" -—–:\t")
    return None


def _skipif_ci_guarded(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when a decorator is pytest.mark.skipif(...) whose condition
    mentions the CI environment switch."""
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        if not (isinstance(func, ast.Attribute) and func.attr == "skipif"):
            continue
        for sub in ast.walk(dec):
            if isinstance(sub, ast.Constant) and sub.value == "CI":
                return True
    return False


def _scan_file(path: Path, rel: Path) -> list[Finding]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:  # fail loud: an unparsable test file is a finding
        return [Finding(rel, exc.lineno or 0, exc.msg, "unparsable test file")]
    lines = src.splitlines()
    findings: list[Finding] = []

    guarded_spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _skipif_ci_guarded(node):
                guarded_spans.append((node.lineno, node.end_lineno or node.lineno))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        if not _upper_bound_wall_clock(node.test):
            continue
        start, end = node.lineno, node.end_lineno or node.lineno
        if any(lo <= start <= hi for lo, hi in guarded_spans):
            continue
        reason = _marker_reason(lines, start, end)
        if reason:
            continue
        why = (
            "hang-guard marker present but missing its mandatory reason"
            if reason is not None
            else "unescaped upper-bound wall-clock assert"
        )
        findings.append(Finding(rel, start, lines[start - 1].strip(), why))
    return findings


def scan_tree(root: Path) -> list[Finding]:
    root = Path(root)
    findings: list[Finding] = []
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return findings
    for path in sorted(tests_dir.rglob("*.py")):
        rel = path.relative_to(root)
        if rel in EXCLUDED_FILES:
            continue
        findings.extend(_scan_file(path, rel))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0]) if args else Path.cwd()
    findings = scan_tree(root)
    if not findings:
        print("wall-clock-asserts: clean")
        return 0
    print(f"wall-clock-asserts: {len(findings)} finding(s)\n")
    for f in findings:
        print(f"  {f.path}:{f.line}  [{f.why}]  {f.text}")
    print()
    print(_TEACHING)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
