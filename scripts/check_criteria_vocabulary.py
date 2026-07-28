#!/usr/bin/env python3
"""Check that deprecated ticket vocabulary remains confined to reviewed records."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = REPO_ROOT / "scripts" / "criteria-vocabulary-allowlist.txt"

# Build this expression from fragments so this checker does not exempt its own source.
PHRASE_PATTERN = re.compile(r"\b" + "success" + r"[ _-]criteri(?:a|on)\b", re.IGNORECASE)
ABBREVIATION_PATTERN = re.compile(r"\bSCs?\b")

EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".tools",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
EXCLUDED_PREFIXES = (Path("docs/archive"), Path("docs/experiments"))
EXCLUDED_FILES = (Path("src/rebar/llm/workflow/editor_assets/dist/editor.js"),)
ABBREVIATION_DIRECTORIES = (Path("src/rebar/llm/reviewers"), Path("src/rebar/_guides"))
ABBREVIATION_FILES = (
    Path(".rebar/criteria_routing.json"),
    Path("src/rebar/llm/plan_review/criteria_routing.json"),
    Path("docs/plan-review-criteria-guide.md"),
)


@dataclass(frozen=True)
class Allowance:
    """A reviewable count pin for one repository-relative file."""

    path: Path
    count: int
    reason: str


@dataclass(frozen=True)
class VocabularyMatch:
    """One deprecated token occurrence, retained for actionable diagnostics."""

    path: Path
    line: int
    text: str
    source_line: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command arguments independently so tests can invoke the checker directly."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root to scan")
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help="tab-delimited path/count/reason exemption file",
    )
    return parser.parse_args(argv)


def _relative_path(raw_path: str, root: Path) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("path must be repository-relative")
    resolved = (root / candidate).resolve()
    try:
        return resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("path escapes the repository root") from error


def load_allowlist(allowlist_path: Path, root: Path) -> tuple[dict[Path, Allowance], list[str]]:
    """Load and validate strict, positive count pins before scanning the repository."""

    if not allowlist_path.is_file():
        return {}, [f"{allowlist_path}: allowlist file does not exist"]

    allowances: dict[Path, Allowance] = {}
    errors: list[str] = []
    for line_number, row in enumerate(allowlist_path.read_text(encoding="utf-8").splitlines(), 1):
        if not row or row.startswith("#"):
            continue
        fields = row.split("\t")
        if len(fields) != 3 or not all(fields):
            errors.append(f"{allowlist_path}:{line_number}: expected path<TAB>count<TAB>reason")
            continue
        raw_path, raw_count, reason = fields
        try:
            path = _relative_path(raw_path, root)
        except ValueError as error:
            errors.append(f"{allowlist_path}:{line_number}: {error}")
            continue
        try:
            count = int(raw_count)
        except ValueError:
            errors.append(f"{allowlist_path}:{line_number}: count must be an integer")
            continue
        if count <= 0:
            errors.append(f"{allowlist_path}:{line_number}: count must be positive")
            continue
        if path in allowances:
            errors.append(f"{allowlist_path}:{line_number}: duplicate path {path}")
            continue
        if not (root / path).is_file():
            errors.append(f"{allowlist_path}:{line_number}: path does not exist: {path}")
            continue
        allowances[path] = Allowance(path=path, count=count, reason=reason)
    return allowances, errors


def _is_excluded(path: Path) -> bool:
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in path.parts):
        return True
    return path in EXCLUDED_FILES or any(
        path.is_relative_to(prefix) for prefix in EXCLUDED_PREFIXES
    )


def _abbreviation_is_scoped(path: Path) -> bool:
    return path in ABBREVIATION_FILES or any(
        path.is_relative_to(directory) for directory in ABBREVIATION_DIRECTORIES
    )


def iter_repository_files(root: Path) -> Iterable[tuple[Path, Path]]:
    """Yield non-symlink text candidates without descending into excluded trees."""

    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        relative_directory = directory.relative_to(root)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not (directory / name).is_symlink() and not _is_excluded(relative_directory / name)
        )
        for file_name in sorted(file_names):
            candidate = directory / file_name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(root)
            if not _is_excluded(relative):
                yield candidate, relative


def _line_matches(abbreviation_is_scoped: bool, line: str) -> Iterable[tuple[int, str]]:
    matches = [(match.start(), match.group()) for match in PHRASE_PATTERN.finditer(line)]
    if abbreviation_is_scoped:
        matches.extend(
            (match.start(), match.group()) for match in ABBREVIATION_PATTERN.finditer(line)
        )
    yield from sorted(matches)


def scan_repository(root: Path) -> dict[Path, list[VocabularyMatch]]:
    """Return every in-scope occurrence grouped by repository-relative path."""

    found: dict[Path, list[VocabularyMatch]] = defaultdict(list)
    for candidate, relative in iter_repository_files(root):
        try:
            text = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        abbreviation_is_scoped = _abbreviation_is_scoped(relative)
        for line_number, line in enumerate(text.splitlines(), 1):
            for _, token in _line_matches(abbreviation_is_scoped, line):
                found[relative].append(VocabularyMatch(relative, line_number, token, line))
    return found


def validate_matches(
    found: dict[Path, list[VocabularyMatch]], allowances: dict[Path, Allowance]
) -> list[str]:
    """Reject new occurrences and allowlist pins that drift in either direction."""

    errors: list[str] = []
    for path, matches in sorted(found.items()):
        if path not in allowances:
            errors.extend(
                f'{match.path}:{match.line}: deprecated vocabulary "{match.text}" '
                f"in {match.source_line}"
                for match in matches
            )
    for path, allowance in sorted(allowances.items()):
        actual = len(found.get(path, []))
        if actual != allowance.count:
            errors.append(
                f"{path}: allowlist count mismatch: expected {allowance.count}, found {actual}"
            )
    return errors


def run(root: Path, allowlist_path: Path, stderr: TextIO = sys.stderr) -> int:
    """Run the guard and write all deterministic diagnostics to *stderr*."""

    root = root.resolve()
    if not root.is_dir():
        print(f"{root}: repository root does not exist", file=stderr)
        return 1
    allowances, errors = load_allowlist(allowlist_path, root)
    if not errors:
        errors.extend(validate_matches(scan_repository(root), allowances))
    if errors:
        print("criteria vocabulary check failed:", file=stderr)
        print(*errors, sep="\n", file=stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the command-line guard."""

    args = parse_args(argv)
    return run(args.root, args.allowlist)


if __name__ == "__main__":
    raise SystemExit(main())
