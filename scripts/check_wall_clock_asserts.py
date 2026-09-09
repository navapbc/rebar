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

The lint also flags unfrozen equality/inequality asserts between
``Path.read_bytes()`` / ``Path.read_text()`` results from different receivers:
that is the transitive flake shape where independently emitted artifacts can
hide wall-clock fields.

Escapes:

- ``# timing: hang-guard — <reason>`` on the assert's lines or the line
  directly above (the reason is MANDATORY — an empty reason still fires):
  sanctions a deliberate stuck-run guard whose ceiling dwarfs the expected
  wall time.
- ``# timing: artifact-equality — <reason>`` on the assert's lines or the line
  directly above (reason mandatory): sanctions a reviewed deterministic
  artifact comparison with no wall-clock-derived bytes.
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
_HANG_GUARD_MARKER = re.compile(r"#\s*timing:\s*hang-guard\s*(?P<reason>.*)$")
_ARTIFACT_EQUALITY_MARKER = re.compile(r"#\s*timing:\s*artifact-equality\s*(?P<reason>.*)$")
_ARTIFACT_READS = {"read_bytes", "read_text"}

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

_ARTIFACT_TEACHING = """\
Unfrozen byte/text equality between independently produced artifacts is a
transitive wall-clock flake proxy: serialized timestamps can differ without a
direct elapsed-time assert. Prefer freezing the production clock seam or
asserting semantic fields. If the artifacts are proven timestamp-free or are a
same-source copy check, sanction the comparison inline with a reason:
    # timing: artifact-equality — <why no wall-clock-derived bytes can differ>"""


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
                    name = (
                        fn.attr
                        if isinstance(fn, ast.Attribute)
                        else (fn.id if isinstance(fn, ast.Name) else "")
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
        for op, comparator in zip(sub.ops, sub.comparators, strict=True):
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


def _marker_reason(lines: list[str], start: int, end: int, marker: re.Pattern[str]) -> str | None:
    """Return the marker reason covering lines start..end or the line above."""
    lo = max(0, start - 2)
    for idx in range(lo, min(end, len(lines))):
        m = marker.search(lines[idx])
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


def _freezes_clock_seam(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "setattr"):
            continue
        if any(
            isinstance(arg, ast.Constant) and arg.value in {"_now_iso", "utc_now_iso"}
            for arg in node.args
        ):
            return True
    return False


def _read_receiver(node: ast.expr) -> tuple[str, str] | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in _ARTIFACT_READS):
        return None
    return (
        func.attr,
        ast.dump(func.value, annotate_fields=True, include_attributes=False),
    )


def _read_assignments(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, tuple[str, str]]:
    reads: dict[str, tuple[str, str]] = {}
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if node.value is None:
            continue
        value = _read_receiver(node.value)
        if value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                reads[target.id] = value
    return reads


def _resolved_read(
    node: ast.expr, assignments: dict[str, tuple[str, str]]
) -> tuple[str, str] | None:
    if isinstance(node, ast.Name):
        return assignments.get(node.id)
    return _read_receiver(node)


def _artifact_equality(test: ast.expr, assignments: dict[str, tuple[str, str]]) -> bool:
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, (ast.Eq, ast.NotEq)):
                left_read = _resolved_read(left, assignments)
                right_read = _resolved_read(comparator, assignments)
                if left_read is not None and right_read is not None and left_read != right_read:
                    return True
            left = comparator
    return False


def _iter_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _scan_file(path: Path, rel: Path) -> list[Finding]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:  # fail loud: an unparsable test file is a finding
        return [Finding(rel, exc.lineno or 0, exc.msg, "unparsable test file")]
    lines = src.split("\n")
    findings: list[Finding] = []

    for fn in _iter_functions(tree):
        skipif_guarded = _skipif_ci_guarded(fn)
        clock_frozen = _freezes_clock_seam(fn)
        assignments = _read_assignments(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            start, end = node.lineno, node.end_lineno or node.lineno
            if _upper_bound_wall_clock(node.test) and not skipif_guarded:
                reason = _marker_reason(lines, start, end, _HANG_GUARD_MARKER)
                if not reason:
                    why = (
                        "hang-guard marker present but missing its mandatory reason"
                        if reason is not None
                        else "unescaped upper-bound wall-clock assert"
                    )
                    findings.append(Finding(rel, start, lines[start - 1].strip(), why))
            if clock_frozen or not _artifact_equality(node.test, assignments):
                continue
            reason = _marker_reason(lines, start, end, _ARTIFACT_EQUALITY_MARKER)
            if reason:
                continue
            why = (
                "artifact-equality marker present but missing its mandatory reason"
                if reason is not None
                else "unescaped cross-artifact read_bytes/read_text equality"
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
    if any("artifact" in f.why for f in findings):
        print()
        print(_ARTIFACT_TEACHING)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
