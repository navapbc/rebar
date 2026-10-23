#!/usr/bin/env python3
"""Shrink-only per-symbol Ruff C901 complexity gate (story c9f7).

``pyproject.toml`` supplies the McCabe threshold (15). Qualified symbol keys
in ``.github/complexity-baseline.json`` record ceilings for legacy findings.
Exit-code contract: raw ``ruff check --select C901 --statistics`` exits 1
for findings; ``--exit-zero --output-format=json`` exits 0 with JSON rows.
``--check`` prints ``active=``/``new=``/``increased=``/``stale=`` counts,
failing for new or increased scores and scanner/schema errors. A stale entry
from reduced or removed complexity passes. Failures add sorted symbol detail.

``--update-stale`` lowers or removes stale ceilings and refuses to write when
new/increased findings or scanner/schema errors exist. The loader requires
``schema_version`` 1 and sorted unique ``ceilings`` keys of the form
``src/rebar/*.py::symbol`` with integer ceilings above 15, plus valid
UTF-8/JSON. Buckets are exclusive: active equals
its ceiling; new lacks a key; increased exceeds its ceiling; stale is absent
or below its ceiling. The CI lock compares against merge-base(main, HEAD)
and fails closed if the base cannot be read."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ``fsutil`` is a stdlib-only leaf; import the canonical atomic writer rather than
# re-implementing the temp-in-same-dir + os.replace dance.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
from rebar._store.fsutil import atomic_write  # noqa: E402

REPO_ROOT = _REPO_ROOT
BASELINE_PATH = REPO_ROOT / ".github" / "complexity-baseline.json"
PRODUCTION_SCOPE = "src/rebar"
COMPLEXITY_THRESHOLD = 15
SCHEMA_VERSION = 1

RUFF_SCAN_ARGS = [
    "ruff",
    "check",
    "--no-cache",
    "--select",
    "C901",
    "--exit-zero",
    "--output-format=json",
    PRODUCTION_SCOPE,
]


class BaselineError(Exception):
    """Base class for every gate failure that must produce a nonzero exit."""


class ScannerError(BaselineError):
    """Ruff could not be run, exited >=2, or produced non-C901 / non-JSON output."""


class NormalizationError(BaselineError):
    """A Ruff finding could not be mapped to a unique qualified symbol."""


class SchemaError(BaselineError):
    """The baseline JSON document violates the Baseline JSON Contract."""


# ─────────────────────────────── scanner ────────────────────────────────────


# raw-git-ok: fixed non-shell ruff C901 census argv (RUFF_SCAN_ARGS), not a git/tracker write
def run_scanner(*, cwd: Path | None = None) -> list[dict]:
    """Run uncached Ruff C901 JSON census using the configured threshold. Parse exit 0/1;
    missing Ruff, exit >=2, invalid JSON, or non-C901 output raises ScannerError."""
    try:
        proc = subprocess.run(  # fixed, non-shell argv
            RUFF_SCAN_ARGS,
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ScannerError(f"ruff executable not found: {exc}") from exc
    except OSError as exc:  # pragma: no cover - defensive
        raise ScannerError(f"could not launch ruff: {exc}") from exc
    if proc.returncode >= 2:
        raise ScannerError(
            f"ruff exited {proc.returncode} (>=2): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return parse_scanner_output(proc.stdout)


def parse_scanner_output(stdout: str) -> list[dict]:
    """Parse Ruff JSON stdout into a finding list, rejecting non-C901 output."""
    try:
        findings = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ScannerError(f"scanner output was not valid JSON: {exc}") from exc
    if not isinstance(findings, list):
        raise ScannerError("scanner output was not a JSON list of findings")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("code") != "C901":
            raise ScannerError("scanner emitted a non-C901 diagnostic")
    return findings


# ──────────────────────────── symbol identity ───────────────────────────────


def build_symbol_index(source: str) -> dict[int, list[tuple[int, str]]]:
    """Index function/async-function qualified names by line and name column. Lambdas are
    excluded; columns distinguish same-line definitions."""
    tree = ast.parse(source)
    index: dict[int, list[tuple[int, str]]] = {}

    def visit(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = [*prefix, child.name]
                keyword_len = 10 if isinstance(child, ast.AsyncFunctionDef) else 4
                name_col = child.col_offset + keyword_len + 1
                index.setdefault(child.lineno, []).append((name_col, ".".join(qual)))
                visit(child, qual)
            elif isinstance(child, ast.ClassDef):
                visit(child, [*prefix, child.name])
            else:
                visit(child, prefix)

    visit(tree, [])
    return index


def resolve_symbol(source: str, row: int, column: int) -> str:
    """Resolve a Ruff (row, column) location to a qualified symbol name."""
    index = build_symbol_index(source)
    candidates = index.get(row, [])
    if not candidates:
        raise NormalizationError(
            f"no function definition at line {row} (lambda or unexpected location)"
        )
    if len(candidates) == 1:
        return candidates[0][1]
    exact = [qual for name_col, qual in candidates if name_col == column]
    if len(exact) == 1:
        return exact[0]
    raise NormalizationError(f"ambiguous function definition at line {row}, column {column}")


def score_from_message(message: str) -> int:
    """Extract the integer complexity score from a C901 message.

    ``"`f` is too complex (16 > 15)"`` -> ``16``.
    """
    match = re.search(r"\((\d+)\s*>\s*\d+\)", message)
    if not match:
        raise NormalizationError(f"could not parse complexity score from: {message!r}")
    return int(match.group(1))


def _repo_relative_posix(filename: str, repo_root: Path) -> str:
    abs_path = Path(filename)
    if not abs_path.is_absolute():
        abs_path = (repo_root / abs_path).resolve()
    try:
        rel = abs_path.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise NormalizationError(
            f"finding path {filename!r} is outside the repository root"
        ) from exc
    return rel.as_posix()


def normalize_findings(findings: list[dict], repo_root: Path) -> dict[str, int]:
    """Normalize Ruff findings into ``{symbol_key: current_score}``.

    Each key is ``<repo-relative-posix-python-path>::<qualified-symbol>``. Two findings
    that resolve to the same qualified key make normalization FAIL (never pick by line
    number or overwrite a ceiling).
    """
    result: dict[str, int] = {}
    source_cache: dict[str, str] = {}
    for finding in findings:
        filename = finding["filename"]
        rel = _repo_relative_posix(filename, repo_root)
        if filename not in source_cache:
            source_cache[filename] = Path(filename).read_text(encoding="utf-8")
        location = finding["location"]
        qual = resolve_symbol(source_cache[filename], int(location["row"]), int(location["column"]))
        key = f"{rel}::{qual}"
        score = score_from_message(finding["message"])
        if key in result:
            raise NormalizationError(f"duplicate normalized symbol key: {key}")
        result[key] = score
    return result


# ──────────────────────────── baseline schema ───────────────────────────────


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict:
    seen: set[str] = set()
    for name, _ in pairs:
        if name in seen:
            raise SchemaError(f"duplicate JSON member name: {name!r}")
        seen.add(name)
    return dict(pairs)


def _validate_key(key: str) -> None:
    if key.count("::") != 1:
        raise SchemaError(f"malformed baseline key (need one '::'): {key!r}")
    path, symbol = key.split("::")
    if "\\" in path:
        raise SchemaError(f"baseline path contains a backslash: {path!r}")
    if path.startswith("/"):
        raise SchemaError(f"baseline path is absolute: {path!r}")
    if not path.startswith("src/rebar/"):
        raise SchemaError(f"baseline path must start with 'src/rebar/': {path!r}")
    if not path.endswith(".py"):
        raise SchemaError(f"baseline path must end with '.py': {path!r}")
    if ".." in path.split("/"):
        raise SchemaError(f"baseline path contains a '..' component: {path!r}")
    if not symbol:
        raise SchemaError(f"baseline key has an empty symbol: {key!r}")
    for segment in symbol.split("."):
        if not segment.isidentifier():
            raise SchemaError(f"symbol segment is not an identifier: {segment!r}")


def _validate_ceiling(key: str, value: object) -> int:
    if type(value) is not int:  # excludes bool (type(True) is not int)
        raise SchemaError(f"ceiling for {key!r} must be a JSON integer, got {value!r}")
    if value <= COMPLEXITY_THRESHOLD:
        raise SchemaError(f"ceiling for {key!r} must be > {COMPLEXITY_THRESHOLD}, got {value}")
    return value


def parse_baseline(raw_bytes: bytes) -> dict[str, int]:
    """Validate and parse baseline document bytes into ``{key: ceiling}``."""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaError(f"baseline is not valid UTF-8: {exc}") from exc
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_members)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"baseline is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise SchemaError("baseline document must be a JSON object")
    if set(doc) != {"schema_version", "ceilings"}:
        raise SchemaError(
            f"baseline must have exactly 'schema_version' and 'ceilings' fields; got {sorted(doc)}"
        )
    if type(doc["schema_version"]) is not int or doc["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(f"schema_version must be the integer {SCHEMA_VERSION}")
    ceilings = doc["ceilings"]
    if not isinstance(ceilings, dict):
        raise SchemaError("'ceilings' must be a JSON object")
    keys = list(ceilings)
    if keys != sorted(keys):
        raise SchemaError("'ceilings' keys must be sorted")
    result: dict[str, int] = {}
    for key, value in ceilings.items():
        _validate_key(key)
        if key in result:  # pragma: no cover - JSON objects cannot repeat keys
            raise SchemaError(f"duplicate normalized key: {key!r}")
        result[key] = _validate_ceiling(key, value)
    return result


def load_baseline(path: Path) -> dict[str, int]:
    """Read and validate the baseline file at ``path``."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise SchemaError(f"baseline file not found: {path}") from exc
    return parse_baseline(raw)


def render_baseline(ceilings: dict[str, int]) -> str:
    """Render a canonical, sorted baseline document (with a trailing newline)."""
    doc = {
        "schema_version": SCHEMA_VERSION,
        "ceilings": {key: ceilings[key] for key in sorted(ceilings)},
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def write_baseline(path: Path, ceilings: dict[str, int]) -> None:
    """Atomically write a canonical baseline via ``fsutil.atomic_write``."""
    atomic_write(path, render_baseline(ceilings))


# ─────────────────────────────── counters ───────────────────────────────────


class Counters:
    """Mutually-exclusive per-symbol classification buckets (lists of keys)."""

    def __init__(self) -> None:
        self.active: list[str] = []
        self.new: list[str] = []
        self.increased: list[str] = []
        self.stale: list[str] = []

    @property
    def summary(self) -> str:
        return (
            f"active={len(self.active)} new={len(self.new)} "
            f"increased={len(self.increased)} stale={len(self.stale)}"
        )

    @property
    def has_regression(self) -> bool:
        return bool(self.new) or bool(self.increased)


def compare(current: dict[str, int], baseline: dict[str, int]) -> Counters:
    """Classify each symbol key into exactly one mutually-exclusive counter."""
    counters = Counters()
    for key, score in current.items():
        if key not in baseline:
            counters.new.append(key)
        elif score > baseline[key]:
            counters.increased.append(key)
        elif score == baseline[key]:
            counters.active.append(key)
        else:  # score < ceiling
            counters.stale.append(key)
    for key in baseline:
        if key not in current:
            counters.stale.append(key)
    return counters


def render_violation_details(
    counters: Counters, current: dict[str, int], baseline: dict[str, int]
) -> list[str]:
    """Return sorted new-score/threshold and increased-score/ceiling lines after a failing
    summary; stale-only checks add none."""
    lines: list[str] = []
    for key in sorted(counters.new):
        lines.append(f"  new       {key} score={current[key]} threshold={COMPLEXITY_THRESHOLD}")
    for key in sorted(counters.increased):
        lines.append(f"  increased {key} score={current[key]} ceiling={baseline[key]}")
    return lines


# ──────────────────────────── update-stale ──────────────────────────────────


def compute_update(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """Return the drained ceilings for ``--update-stale`` (caller guards regressions).

    Lowers still-over-threshold ceilings to the current score, drops entries with no
    current diagnostic, and preserves active entries unchanged.
    """
    updated: dict[str, int] = {}
    for key, ceiling in baseline.items():
        if key not in current:
            continue  # removed diagnostic -> drop
        score = current[key]
        updated[key] = score if score < ceiling else ceiling
    return updated


# ───────────────────────────────── CLI ──────────────────────────────────────


def _run_check(argv_cwd: Path) -> int:
    try:
        findings = run_scanner(cwd=argv_cwd)
        current = normalize_findings(findings, argv_cwd)
        baseline = load_baseline(BASELINE_PATH)
    except BaselineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    counters = compare(current, baseline)
    print(counters.summary)
    for line in render_violation_details(counters, current, baseline):
        print(line)
    return 1 if counters.has_regression else 0


def _run_update_stale(argv_cwd: Path) -> int:
    try:
        findings = run_scanner(cwd=argv_cwd)
        current = normalize_findings(findings, argv_cwd)
        baseline = load_baseline(BASELINE_PATH)
    except BaselineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    counters = compare(current, baseline)
    if counters.has_regression:
        print(
            f"refusing to update: baseline has new/increased debt ({counters.summary})",
            file=sys.stderr,
        )
        return 1
    updated = compute_update(current, baseline)
    write_baseline(BASELINE_PATH, updated)
    post = compare(current, updated)
    print(post.summary)
    return 0


# ───────────────────────────── lock gate ────────────────────────────────────


def lock_against_base(base: dict[str, int], branch: dict[str, int]) -> list[str]:
    """Report newly added or raised ceilings against the base. Removed or lowered entries pass."""
    violations: list[str] = []
    for key, branch_ceiling in branch.items():
        if key not in base:
            violations.append(
                f"NEW entry added: {key!r} (ceiling {branch_ceiling}) — "
                "an administrator must override the gate to add a new baseline entry"
            )
        elif branch_ceiling > base[key]:
            violations.append(
                f"ceiling RAISED for {key!r}: {base[key]} -> {branch_ceiling} — "
                "an administrator must override the gate to raise a ceiling"
            )
    return violations


_BASELINE_REPO_RELPATH = ".github/complexity-baseline.json"


class LockFetchError(BaselineError):
    """The base (main) copy of the baseline could not be established (fail closed)."""


def _base_commit_for_comparison(main_ref: str) -> str:
    """Use merge-base(main, HEAD) so later main reductions do not penalize an unchanged branch.
    If unavailable, compare against main's stricter tip and fail closed (bug
    humble-cinnamoned-mussel)."""
    if (
        subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        == "true"
    ):
        # A shallow HEAD may not reach the fork point; deepen before giving up. Best-effort:
        # a failure here just falls through to the merge-base attempt below.
        subprocess.run(
            ["git", "fetch", "--unshallow"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    merge_base = subprocess.run(
        ["git", "merge-base", main_ref, "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    rev = merge_base.stdout.strip()
    if merge_base.returncode != 0 or not rev:
        print(
            "complexity-baseline lock: no merge base with main could be computed; "
            "comparing against main's tip instead (stricter, so a genuine raise still fails)."
        )
        return main_ref
    return rev


def _fetch_base_ceilings_from_main() -> dict[str, int] | None:
    """Fetch canonical GitHub main by explicit URL, then parse the merge-base ceilings. A
    missing baseline on main returns None; missing repository, fetch, or parse failure
    raises LockFetchError."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        raise LockFetchError(
            "GITHUB_REPOSITORY is not set; cannot fetch main to verify the "
            "complexity-baseline lock (failing closed)"
        )
    url = f"https://github.com/{repo}"
    ref = "refs/remotes/gh-main-complexity-lock"
    # Fetch enough history for merge-base; depth 1 cannot distinguish a branch
    # ceiling raise from a later reduction on main (bug humble-cinnamoned-mussel).
    fetch = subprocess.run(  # fixed, non-shell argv
        ["git", "fetch", url, f"+refs/heads/main:{ref}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if fetch.returncode != 0:
        raise LockFetchError(
            f"could not fetch main from {url} to verify the complexity-baseline lock "
            f"(failing closed): {fetch.stderr.strip()}"
        )
    rev = _base_commit_for_comparison(ref)
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}:{_BASELINE_REPO_RELPATH}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode != 0:
        return None  # main has no baseline yet — bootstrap, allow
    show = subprocess.run(
        ["git", "show", f"{rev}:{_BASELINE_REPO_RELPATH}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )
    if show.returncode != 0:
        raise LockFetchError(
            "could not read main's copy of the baseline (failing closed): "
            f"{show.stderr.decode('utf-8', 'replace').strip()}"
        )
    try:
        return parse_baseline(show.stdout)
    except SchemaError as exc:
        raise LockFetchError(f"main's baseline is unreadable (failing closed): {exc}") from exc


def _run_lock(base_path: Path | None) -> int:
    base: dict[str, int] | None
    if base_path is not None:
        try:
            base = load_baseline(base_path)
        except SchemaError as exc:
            print(
                f"error: could not load base baseline from {base_path}: {exc}",
                file=sys.stderr,
            )
            return 1
    else:
        try:
            base = _fetch_base_ceilings_from_main()
        except LockFetchError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            return 1
    if base is None:
        print("complexity-baseline lock: main has no baseline yet — bootstrap, allowing.")
        return 0
    try:
        branch = load_baseline(BASELINE_PATH)
    except SchemaError as exc:
        print(f"error: could not load branch baseline: {exc}", file=sys.stderr)
        return 1
    violations = lock_against_base(base, branch)
    if violations:
        for v in violations:
            print(f"complexity-baseline lock: {v}")
        print(
            "hint: an administrator must override the Verified gate (force-submit) "
            "to land a raised ceiling or a new baseline entry."
        )
        return 1
    print("complexity-baseline lock: OK (no raised ceilings or new entries)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_complexity_baseline.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check",
        action="store_true",
        help=(
            "Gate mode. Scan src/rebar for C901 findings and compare against the "
            "committed baseline. Exit 0 only when no finding is new or increased; "
            "print 'active=<N> new=<N> increased=<N> stale=<N>'. stale>0 alone is an "
            "allowed improvement (exit 0)."
        ),
    )
    group.add_argument(
        "--update-stale",
        action="store_true",
        help=(
            "Maintenance rewrite. Lower still-over-threshold ceilings and remove "
            "entries whose diagnostic vanished, then atomically write canonical sorted "
            "JSON. REFUSES to write (nonzero) when new>0 or increased>0 or on any "
            "scanner/schema error, so it can never bless regressions."
        ),
    )
    group.add_argument(
        "--lock",
        action="store_true",
        help=(
            "Lock gate. Semantically compare the branch baseline against the base "
            "(main) copy. Without --base, fetch main by explicit URL "
            "(GITHUB_REPOSITORY). Exits nonzero if any ceiling is raised or any new "
            "entry is added. Fails closed if the base cannot be established."
        ),
    )
    parser.add_argument(
        "--base",
        metavar="PATH",
        type=Path,
        help=(
            "Optional explicit path to the base (main) copy of the baseline JSON for "
            "--lock. When omitted, --lock fetches main itself."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.lock:
        return _run_lock(args.base)
    if args.update_stale:
        return _run_update_stale(REPO_ROOT)
    return _run_check(REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
