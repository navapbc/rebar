#!/usr/bin/env python3
"""Check that deprecated ticket vocabulary remains confined to reviewed records."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
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

# Only the FALLBACK walk (a non-git root) needs a hand-maintained denylist. Inside a git
# repository the guard asks git for the file list instead, so `.gitignore` — not this set — is
# the single source of truth for what is out of scope. That distinction is the whole point of
# bug d5ae: a denylist over an open-ended filesystem drifts, and when it drifted past
# `.claude/worktrees/` and `bridge_state/` it produced ~47k violations in gitignored paths and
# permanently reddened the `make lint` commit gate for anyone who had created a worktree.
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
# ``scripts/build_cloud_adf_corpus.py`` NAMES the retired vocabulary because its
# scrub MAPS it to the canonical spellings when freezing captured ticket prose into
# a fixture — the same reason ``scripts/gen_cli_reference.py`` is a compatibility
# file for the bridge-vocabulary contract.
EXCLUDED_FILES = (
    Path("src/rebar/llm/workflow/editor_assets/dist/editor.js"),
    Path("scripts/build_cloud_adf_corpus.py"),
)
ABBREVIATION_DIRECTORIES = (Path("src/rebar/llm/reviewers"), Path("src/rebar/_guides"))
ABBREVIATION_FILES = (
    Path(".rebar/criteria_routing.json"),
    Path("src/rebar/llm/plan_review/criteria_routing.json"),
    Path("docs/plan-review-criteria-guide.md"),
)

# Decode policy. A guard that silently skips whatever it cannot decode is a guard with an
# opt-out: one stray byte in a source file would hide every occurrence in it. So the suffixes
# below — the repository's own authored text formats, where an undecodable byte is a defect
# rather than a legitimate payload — FAIL CLOSED: the file is reported as an error instead of
# skipped. Every other suffix stays tolerated, because genuine binaries (images, archives,
# compiled artifacts) are checked in legitimately and must not turn the gate red. The split is
# deliberately by extension, not by content sniffing: it is reviewable, stable, and cannot be
# defeated by crafting bytes that a heuristic would call binary.
SOURCE_TEXT_SUFFIXES = frozenset({".py", ".md", ".json", ".toml", ".yaml", ".yml"})

# Diagnostic excerpts are attacker-influenced text going straight into a CI log, so they are
# escaped (no raw control characters, no terminal escape sequences) and capped. The cap applies
# to the ESCAPED form so the emitted width is bounded no matter how the input expands. A
# truncated excerpt still identifies the site exactly: path, line, and the matched token are
# reported separately from the excerpt.
MAX_EXCERPT_CHARS = 200
TRUNCATION_MARKER = "… [truncated]"


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
    excerpt: str
    """The matching line, already escaped and truncated by `_excerpt`."""


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


def _git_tracked_relatives(root: Path) -> list[Path] | None:
    """Return the paths git would consider, or None when *root* is not a usable git repo.

    `--cached --others --exclude-standard` is precisely "tracked, plus untracked files that
    are not ignored" — so a brand-new file is still scanned the moment it is written, while
    anything `.gitignore` excludes is out of scope by construction.
    """

    if not (root / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return [Path(os.fsdecode(entry)) for entry in completed.stdout.split(b"\0") if entry]


def _iter_listed_files(root: Path, relatives: list[Path]) -> Iterable[tuple[Path, Path]]:
    for relative in sorted(set(relatives)):
        if _is_excluded(relative):
            continue
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file():
            continue
        yield candidate, relative


def iter_repository_files(root: Path) -> Iterable[tuple[Path, Path]]:
    """Yield non-symlink text candidates, preferring git's own view of the tree."""

    listed = _git_tracked_relatives(root)
    if listed is not None:
        yield from _iter_listed_files(root, listed)
        return
    yield from _walk_repository_files(root)


def _walk_repository_files(root: Path) -> Iterable[tuple[Path, Path]]:
    """Fallback for a non-git root: walk without descending into excluded trees."""

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


def _escape(text: str) -> str:
    """Render *text* with every non-printable character as a visible escape."""

    return "".join(
        character
        if character.isprintable()
        else (
            f"\\x{ord(character):02x}"
            if ord(character) < 0x100
            else f"\\u{ord(character):04x}"
            if ord(character) <= 0xFFFF
            else f"\\U{ord(character):08x}"
        )
        for character in text
    )


def _excerpt(line: str) -> str:
    """Escape and length-cap one source line for safe inclusion in a diagnostic."""

    escaped = _escape(line)
    if len(escaped) <= MAX_EXCERPT_CHARS:
        return escaped
    return escaped[:MAX_EXCERPT_CHARS] + TRUNCATION_MARKER


def _fails_closed_on_decode_error(relative: Path) -> bool:
    return relative.suffix.lower() in SOURCE_TEXT_SUFFIXES


def scan_repository(root: Path) -> tuple[dict[Path, list[VocabularyMatch]], list[str]]:
    """Return every in-scope occurrence by path, plus errors for undecodable source files."""

    found: dict[Path, list[VocabularyMatch]] = defaultdict(list)
    decode_errors: list[str] = []
    for candidate, relative in iter_repository_files(root):
        try:
            text = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Fail closed for authored text formats; tolerate everything else as binary.
            if _fails_closed_on_decode_error(relative):
                decode_errors.append(
                    f"{relative}: is not valid UTF-8, so it cannot be checked for deprecated "
                    "vocabulary; fix the encoding or rename it out of "
                    f"{sorted(SOURCE_TEXT_SUFFIXES)}"
                )
            continue
        abbreviation_is_scoped = _abbreviation_is_scoped(relative)
        for line_number, line in enumerate(text.splitlines(), 1):
            for _, token in _line_matches(abbreviation_is_scoped, line):
                found[relative].append(
                    VocabularyMatch(relative, line_number, token, _excerpt(line))
                )
    return found, sorted(decode_errors)


def validate_matches(
    found: dict[Path, list[VocabularyMatch]], allowances: dict[Path, Allowance]
) -> list[str]:
    """Reject new occurrences and allowlist pins that drift in either direction."""

    errors: list[str] = []
    for path, matches in sorted(found.items()):
        if path not in allowances:
            errors.extend(
                f'{_escape(str(match.path))}:{match.line}: deprecated vocabulary "{match.text}" '
                f"in {match.excerpt}"
                for match in matches
            )
    for path, allowance in sorted(allowances.items()):
        actual = len(found.get(path, []))
        if actual != allowance.count:
            errors.append(
                f"{_escape(str(path))}: allowlist count mismatch: "
                f"expected {allowance.count}, found {actual}"
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
        found, decode_errors = scan_repository(root)
        errors.extend(decode_errors)
        errors.extend(validate_matches(found, allowances))
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
