#!/usr/bin/env python3
"""Comment-hygiene guard: block rot-prone history in comments and docstrings.

Policy [rebar:b047-267f-3c3d-4374]: source comments carry CURRENT STATE and concise
rationale; the ticket system owns historical detail. Commit SHAs rot (Gerrit's
rebase-on-submit guarantees the pre-land SHA dies at merge), run/job/thread ids die
with retention windows, and dated incident narratives go stale silently. This guard
fails CI when a comment block or docstring narrates history through such tokens
WITHOUT pointing at the ticket system.

A block that TRIGGERS (bare SHA-like token, run-id-after-keyword, or a dated
incident narrative) is ACCEPTED when it cites a durable reference: a grouped hex
ticket id (``3006-e198`` form), a word-triple store alias (``robe-creek-zealot``),
an ADR id, or the vendor-ref escape hatch ``context: external`` (for upstream
changelogs / issue SHAs that rebar's ticket system does not own).

ACCEPTED RESIDUAL: only Python comment tokens and docstrings are scanned — string
literals are runtime data, not policy surface. The guard's own fixture corpus
(EXCLUDED_FILES below) is structurally excluded: those fixtures MUST contain live
rot-prone tokens to stay red/green honest, and keeping them in exactly one excluded
module is what lets the tree scan demand zero suppressions.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCANNED_TREES = ("src", "tests", "scripts")

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

# The guard's own unit-test module is its fixture corpus: it deliberately holds live
# rot-prone tokens as RED/gotcha cases, so it is the ONE structural exclusion
# (the check_criteria_vocabulary.py EXCLUDED_FILES idiom).
EXCLUDED_FILES = (Path("tests/unit/test_comment_hygiene_guard.py"),)

# ── triggers ──────────────────────────────────────────────────────────────────────

# 7-40 hex chars carrying BOTH digits and letters (a pure number is not a SHA; a
# pure-letter hex word like "decade" is prose).
_SHA_TOKEN = re.compile(r"\b[0-9a-f]{7,40}\b")

# Hash/signature algorithm names that are themselves valid hex strings.
_ALGORITHM_DENYLIST = frozenset({"ed25519", "25519", "b2b256"})

# A retention-bound automation id: >=8 digits directly following its keyword.
_RUN_ID = re.compile(r"(?i)\b(?:run|job|thread|build|workflow|pipeline)[\s#:=-]{0,4}(\d{8,})\b")

_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+\d{1,2},\s+\d{4}\b"
)

# The ENUMERATED evidence-verb set. 'surfaced' is deliberately EXCLUDED: "a bug
# surfaced ..." introduces current-state explanation, not an incident date.
_EVIDENCE_VERB = re.compile(
    r"(?i)\b(?:failed|broke|regressed|fixed|caught|observed|hit|crashed|flaked)\b"
)

# ── acceptors ─────────────────────────────────────────────────────────────────────

# Grouped hex ticket id: 2-4 groups of 4 hex chars (3006-e198 / full 4x4 ids).
_GROUPED_HEX_ID = re.compile(r"\b[0-9a-f]{4}(?:-[0-9a-f]{4}){1,3}\b")

# Word-triple store alias: exactly three hyphen-joined lowercase words, not embedded
# in a longer hyphen chain, and NOT path/filename context — a hyphenated file name like
# docs/designs/sync-hardening-proposal.md must not accept a block (it masked a live raw
# SHA in the b047 close verification). Rejects a preceding path separator and a trailing
# file extension; a sentence-final alias ("…robe-creek-zealot.") still accepts because
# the extension shape requires letters after the dot.
_WORD_TRIPLE_ALIAS = re.compile(
    r"(?<![a-z0-9./\\-])[a-z]{3,}-[a-z]{3,}-[a-z]{3,}(?![a-z0-9-])(?!\.[a-z]{1,4}\b)"
)

_ADR_ID = re.compile(r"(?i)\bADR[- ]?\d{3,4}\b")

_ESCAPE_HATCH = re.compile(r"(?i)\bcontext:\s*external\b")

_TEACHING = (
    "source comments carry current state; the ticket system owns history. Either "
    "cite a resolvable ticket/epic/story/bug id (grouped hex or word-triple alias) "
    "or an ADR, drop the rot-prone token in favor of durable prose, or mark a "
    "vendor-pinned reference with 'context: external'."
)


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    token: str
    kind: str


def _sha_hits(text: str) -> list[str]:
    hits = []
    for match in _SHA_TOKEN.finditer(text):
        token = match.group(0)
        if token in _ALGORITHM_DENYLIST or token.isdigit() or token.isalpha():
            continue
        hits.append(token)
    return hits


def _block_triggers(text: str) -> list[tuple[str, str]]:
    """Return (kind, token) trigger hits for one comment block / docstring."""
    triggers = [("commit-sha", token) for token in _sha_hits(text)]
    triggers.extend(("run-id", match.group(1)) for match in _RUN_ID.finditer(text))
    date = _DATE.search(text)
    verb = _EVIDENCE_VERB.search(text)
    if date and verb:
        triggers.append(("dated-incident", f"{date.group(0)} + {verb.group(0)}"))
    return triggers


def _block_accepted(text: str) -> bool:
    return bool(
        _GROUPED_HEX_ID.search(text)
        or _WORD_TRIPLE_ALIAS.search(text)
        or _ADR_ID.search(text)
        or _ESCAPE_HATCH.search(text)
    )


def _comment_blocks(source: str) -> list[tuple[int, str]]:
    """(first_line, text) for each run of adjacent comment lines, via tokenize so
    '#' inside string literals never reads as a comment."""
    blocks: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_start = 0
    last_line = -2
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            line = tok.start[0]
            if line == last_line + 1 and current_lines:
                current_lines.append(tok.string)
            else:
                if current_lines:
                    blocks.append((current_start, "\n".join(current_lines)))
                current_lines = [tok.string]
                current_start = line
            last_line = line
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    if current_lines:
        blocks.append((current_start, "\n".join(current_lines)))
    return blocks


def _docstring_blocks(source: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    blocks: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            blocks.append((body[0].lineno, body[0].value.value))
    return blocks


def _scan_file(path: Path, relative: Path) -> list[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    violations: list[Violation] = []
    for start, text in _comment_blocks(source) + _docstring_blocks(source):
        triggers = _block_triggers(text)
        if not triggers or _block_accepted(text):
            continue
        violations.extend(
            Violation(path=relative, line=start, token=token, kind=kind) for kind, token in triggers
        )
    return violations


def _iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for tree_name in SCANNED_TREES:
        tree = root / tree_name
        if not tree.is_dir():
            continue
        for path in sorted(tree.rglob("*.py")):
            if any(part in EXCLUDED_DIRECTORY_NAMES for part in path.parts):
                continue
            if path.relative_to(root) in EXCLUDED_FILES:
                continue
            files.append(path)
    return files


def check(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in _iter_python_files(root):
        violations.extend(_scan_file(path, path.relative_to(root)))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    violations = check(args.root.resolve())
    if not violations:
        return 0
    print(f"comment-hygiene: {len(violations)} rot-prone reference(s) found\n")
    for violation in violations:
        print(f"  {violation.path}:{violation.line}  [{violation.kind}]  {violation.token}")
    print(f"\n{_TEACHING}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
