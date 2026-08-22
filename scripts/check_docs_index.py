#!/usr/bin/env python3
"""Validate documentation index membership and repository-relative targets."""

from __future__ import annotations

import argparse
import os
import re
import string
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS_DIR = REPO_ROOT / "docs"

INDEX_NAME = "README.md"

# These paths contain maintained Markdown sources. Paths are repository-relative.
MARKDOWN_SOURCE_TREES = (
    ".agents",
    ".github",
    "docs",
    "examples/agent-skills",
    "infra/runbooks",
    "src/rebar/_guides",
    "src/rebar/llm/eval_specs",
    "templates",
)
MARKDOWN_SOURCE_GLOBS = (
    "infra/**/README.md",
    "scripts/**/README.md",
    "tests/external/**/README.md",
)
MARKDOWN_SOURCE_FILES = ("tests/unit/fixtures/README.md",)
MARKDOWN_SOURCE_EXCLUSIONS = (
    ".joe-janitor",
    ".rebar/prompts",
    "src/rebar/llm/reviewers",
    "tests/fixtures",
    "tests/scripts/fixtures",
    "tests/unit/rebar_reconciler/integration_gates",
)

# Index membership retains the original target extraction behavior.
_INDEX_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_QUALIFIER_RE = re.compile(r"[?#]")


class LinkFinding(NamedTuple):
    """One repository-relative Markdown target that cannot resolve."""

    source_path: str
    line_number: int
    raw_target: str
    normalized_target_path: str
    reason: str


def _link_targets(text: str) -> list[str]:
    """Return target text from index membership links."""
    return [match.group(1).strip() for match in _INDEX_LINK_RE.finditer(text)]


def find_unindexed(docs_dir: Path) -> list[str]:
    """Return sorted top-level Markdown files absent from the documentation index.

    The index and files ending in ``.local.md`` are outside this rule. A bare prose
    mention does not establish index membership.
    """
    index_path = docs_dir / INDEX_NAME
    index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    linked: set[str] = set()
    for target in _link_targets(index_text):
        target = target.split("#", 1)[0]
        if target.startswith("./"):
            target = target[2:]
        linked.add(target)

    unindexed: list[str] = []
    for markdown_path in docs_dir.glob("*.md"):
        name = markdown_path.name
        if name == INDEX_NAME or markdown_path.match("*.local.md"):
            continue
        if name not in linked:
            unindexed.append(name)
    return sorted(unindexed)


def _has_path_prefix(path: Path, prefix: str) -> bool:
    prefix_parts = Path(prefix).parts
    return path.parts[: len(prefix_parts)] == prefix_parts


def _is_excluded_source(path: Path, repo_root: Path) -> bool:
    relative = path.relative_to(repo_root)
    if relative.parent == Path() and relative.name.startswith("."):
        return True
    if relative.name.endswith(".local.md"):
        return True
    return any(_has_path_prefix(relative, prefix) for prefix in MARKDOWN_SOURCE_EXCLUSIONS)


def find_markdown_sources(repo_root: Path) -> list[Path]:
    """Return the sorted maintained Markdown source boundary."""
    root = repo_root.resolve()
    candidates: set[Path] = set()

    candidates.update(path for path in root.glob("*.md") if path.is_file())
    for relative_tree in MARKDOWN_SOURCE_TREES:
        tree = root / relative_tree
        if tree.is_dir():
            candidates.update(path for path in tree.rglob("*.md") if path.is_file())
    for pattern in MARKDOWN_SOURCE_GLOBS:
        candidates.update(path for path in root.glob(pattern) if path.is_file())
    for relative_file in MARKDOWN_SOURCE_FILES:
        path = root / relative_file
        if path.is_file():
            candidates.add(path)

    included = [path for path in candidates if not _is_excluded_source(path, root)]
    return sorted(included, key=lambda path: path.relative_to(root).as_posix())


def _mask_fenced_blocks(text: str) -> str:
    """Replace fenced blocks with spaces while preserving line offsets."""
    masked = list(text)
    fence_character: str | None = None
    fence_width = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        fence = _FENCE_RE.match(content)
        mask_line = fence_character is not None or fence is not None
        if fence_character is not None:
            if fence:
                marker = fence.group("marker")
                if (
                    marker.startswith(fence_character)
                    and len(marker) >= fence_width
                    and not content[fence.end() :].strip()
                ):
                    fence_character = None
                    fence_width = 0
        elif fence:
            marker = fence.group("marker")
            fence_character = marker[0]
            fence_width = len(marker)
        if mask_line:
            masked[offset : offset + len(content)] = " " * len(content)
        offset += len(line)
    return "".join(masked)


def _mask_inline_code(text: str) -> str:
    """Replace complete code spans with spaces while preserving offsets."""
    runs: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(text):
        if text[cursor] != "`":
            cursor += 1
            continue
        end = cursor + 1
        while end < len(text) and text[end] == "`":
            end += 1
        runs.append((cursor, end))
        cursor = end

    masked = list(text)
    run_index = 0
    while run_index < len(runs):
        start, opening_end = runs[run_index]
        width = opening_end - start
        closing_index = next(
            (
                candidate_index
                for candidate_index in range(run_index + 1, len(runs))
                if runs[candidate_index][1] - runs[candidate_index][0] == width
            ),
            None,
        )
        if closing_index is None:
            run_index += 1
            continue
        closing_end = runs[closing_index][1]
        masked[start:closing_end] = " " * (closing_end - start)
        run_index = closing_index + 1
    return "".join(masked)


def _closing_parenthesis(text: str, start: int) -> int | None:
    depth = 0
    in_angle_destination = False
    cursor = start
    while cursor < len(text):
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "<" and depth == 0:
            in_angle_destination = True
        elif character == ">" and in_angle_destination:
            in_angle_destination = False
        elif character == "(" and not in_angle_destination:
            depth += 1
        elif character == ")" and not in_angle_destination:
            if depth == 0:
                return cursor
            depth -= 1
        cursor += 1
    return None


def _destination(link_body: str) -> str:
    body = link_body.strip()
    if not body:
        return ""
    if body.startswith("<"):
        end = body.find(">", 1)
        return body[1:end] if end != -1 else ""

    cursor = 0
    while cursor < len(body):
        if body[cursor] == "\\" and cursor + 1 < len(body):
            cursor += 2
            continue
        if body[cursor].isspace():
            break
        cursor += 1
    return body[:cursor]


def _inline_targets(text: str) -> Iterator[tuple[int, str]]:
    bracket_stack: list[int] = []
    cursor = 0
    while cursor < len(text):
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "[":
            bracket_stack.append(cursor)
            cursor += 1
            continue
        if character != "]" or not bracket_stack:
            cursor += 1
            continue

        opening = bracket_stack.pop()
        if cursor + 1 >= len(text) or text[cursor + 1] != "(":
            cursor += 1
            continue
        closing = _closing_parenthesis(text, cursor + 2)
        if closing is None:
            cursor += 1
            continue
        target = _destination(text[cursor + 2 : closing])
        if target:
            yield opening, target
        cursor = closing + 1


def _document_targets(text: str) -> Iterator[tuple[int, str]]:
    visible = _mask_inline_code(_mask_fenced_blocks(text))
    for offset, target in _inline_targets(visible):
        yield text.count("\n", 0, offset) + 1, target


def _unescape_destination(target: str) -> str:
    characters: list[str] = []
    cursor = 0
    while cursor < len(target):
        if (
            target[cursor] == "\\"
            and cursor + 1 < len(target)
            and target[cursor + 1] in string.punctuation
        ):
            characters.append(target[cursor + 1])
            cursor += 2
            continue
        characters.append(target[cursor])
        cursor += 1
    return "".join(characters)


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return Path(os.path.relpath(path, repo_root)).as_posix()


def _finding_for_target(
    source: Path,
    source_path: str,
    line_number: int,
    raw_target: str,
    repo_root: Path,
) -> LinkFinding | None:
    if raw_target.startswith("#") or _SCHEME_RE.match(raw_target):
        return None

    path_part = _QUALIFIER_RE.split(raw_target, maxsplit=1)[0]
    if not path_part:
        return None
    resolved = (source.parent / _unescape_destination(path_part)).resolve()
    normalized = _display_path(resolved, repo_root)
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        return LinkFinding(
            source_path,
            line_number,
            raw_target,
            normalized,
            "outside-repository",
        )
    if not resolved.exists():
        return LinkFinding(
            source_path,
            line_number,
            raw_target,
            normalized,
            "missing-target",
        )
    return None


def find_link_findings(
    repo_root: Path,
    sources: Iterable[Path] | None = None,
) -> list[LinkFinding]:
    """Return sorted failures for inline relative links and images."""
    root = repo_root.resolve()
    selected = list(sources) if sources is not None else find_markdown_sources(root)
    findings: list[LinkFinding] = []
    for source in sorted(selected, key=lambda path: path.relative_to(root).as_posix()):
        source_path = source.relative_to(root).as_posix()
        text = source.read_text(encoding="utf-8")
        for line_number, raw_target in _document_targets(text):
            finding = _finding_for_target(
                source,
                source_path,
                line_number,
                raw_target,
                root,
            )
            if finding is not None:
                findings.append(finding)
    return sorted(findings)


def find_broken_links(docs_dir: Path) -> list[tuple[str, str]]:
    """Return the compatibility view for Markdown sources below ``docs_dir``."""
    docs_root = docs_dir.resolve()
    repo_root = docs_root.parent
    sources = sorted(path for path in docs_root.rglob("*.md") if path.is_file())
    return [
        (Path(finding.source_path).name, finding.raw_target)
        for finding in find_link_findings(repo_root, sources)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check documentation index membership and repository-relative targets."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when index or link findings exist.",
    )
    parser.parse_args(argv)

    docs_dir = DEFAULT_DOCS_DIR
    repo_root = docs_dir.resolve().parent
    unindexed = find_unindexed(docs_dir)
    findings = find_link_findings(repo_root)

    for name in unindexed:
        sys.stderr.write(f"Unindexed documentation file {name}. Add it to {INDEX_NAME}.\n")
    for finding in findings:
        sys.stderr.write(
            f"Broken Markdown target in {finding.source_path} at line "
            f"{finding.line_number}. Raw target {finding.raw_target!r}. "
            f"Normalized target {finding.normalized_target_path!r}. "
            f"Reason {finding.reason}.\n"
        )

    return 1 if unindexed or findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
