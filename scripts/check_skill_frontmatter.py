#!/usr/bin/env python3
"""SKILL.md frontmatter lint (ticket db04).

GitHub Copilot CLI (and the Agent Skills spec at agentskills.io) enforce hard
limits on a skill's YAML frontmatter, but Copilot CLI drops a non-conforming
skill *silently* — no error, no warning, the skill simply never registers
(github/copilot-cli#3494 for the 1024-char description cap; #1024 for the strict
YAML parse). This check makes that failure class loud at commit/CI time instead.

The rules mirror Anthropic's reference validator
(anthropics/skills:skills/skill-creator/scripts/quick_validate.py):

  - frontmatter must parse as a YAML mapping;
  - ``description`` is required, a non-empty string, <= 1024 characters, and must
    not contain angle brackets (``<`` / ``>``);
  - ``name`` is OPTIONAL (Copilot CLI derives it from the directory when absent),
    but when present must be a string <= 64 characters matching
    ``^[a-z0-9]+(-[a-z0-9]+)*$``.

Lengths are counted in characters, not bytes, matching the spec.

API:
  - find_skill_files(root: Path) -> list[Path]
  - check_file(path: Path) -> list[str]         # [] means OK
  - main(argv: list[str] | None = None) -> int  # 0 = all valid

Usage:
  python scripts/check_skill_frontmatter.py                 # scan examples/agent-skills
  python scripts/check_skill_frontmatter.py path/to/SKILL.md ...
  python scripts/check_skill_frontmatter.py --github        # emit ::error:: annotations
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO_ROOT / "examples" / "agent-skills"

NAME_MAX_LENGTH = 64
DESCRIPTION_MAX_LENGTH = 1024
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def find_skill_files(root: Path) -> list[Path]:
    """Return every ``SKILL.md`` at or under ``root`` (sorted, stable)."""
    if root.is_file():
        return [root]
    return sorted(root.glob("**/SKILL.md"))


def _extract_frontmatter(text: str) -> str | None:
    """Return the raw YAML frontmatter block, or ``None`` if absent."""
    m = _FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def check_file(path: Path) -> list[str]:
    """Validate one SKILL.md. Return a list of human-readable problems ([]=OK)."""
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")

    raw = _extract_frontmatter(text)
    if raw is None:
        return ["missing YAML frontmatter (a leading '---' block is required)"]

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        return [f"frontmatter is not valid YAML: {detail}"]

    if not isinstance(data, dict):
        return ["frontmatter did not parse to a mapping (check for an unquoted ': ')"]

    problems.extend(_check_description(data))
    problems.extend(_check_name(data))
    return problems


def _check_description(data: dict) -> list[str]:
    """Validate the required ``description`` field."""
    if "description" not in data:
        return ["missing required 'description'"]
    desc = data["description"]
    if not isinstance(desc, str):
        return [
            f"'description' must be a string (got {type(desc).__name__}; "
            "check for an unquoted ': ' turning it into a mapping)"
        ]
    problems: list[str] = []
    if not desc.strip():
        problems.append("'description' must be non-empty")
    if len(desc) > DESCRIPTION_MAX_LENGTH:
        problems.append(
            f"'description' is {len(desc)} characters; the maximum is "
            f"{DESCRIPTION_MAX_LENGTH} (Copilot CLI silently drops the skill)"
        )
    if "<" in desc or ">" in desc:
        problems.append("'description' must not contain angle brackets ('<' or '>')")
    return problems


def _check_name(data: dict) -> list[str]:
    """Validate the optional ``name`` field when present."""
    if "name" not in data:
        return []
    name = data["name"]
    if not isinstance(name, str):
        return [f"'name' must be a string (got {type(name).__name__})"]
    problems: list[str] = []
    if len(name) > NAME_MAX_LENGTH:
        problems.append(f"'name' is {len(name)} characters; the maximum is {NAME_MAX_LENGTH}")
    if not _NAME_RE.match(name):
        problems.append(
            f"'name' {name!r} must be kebab-case: lowercase letters, digits, and "
            "single hyphens, with no leading/trailing/consecutive hyphens"
        )
    return problems


def _emit_github(path: Path, problem: str) -> str:
    rel = os.path.relpath(path, REPO_ROOT)
    msg = problem.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error file={rel},title=skill-frontmatter::{msg}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Agent Skills SKILL.md frontmatter (name/description limits)."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="SKILL.md files or directories to scan (default: examples/agent-skills)",
    )
    parser.add_argument(
        "--github",
        action="store_true",
        help="emit GitHub Actions ::error:: annotations for each problem",
    )
    args = parser.parse_args(argv)

    roots = args.paths or [DEFAULT_ROOT]
    files: list[Path] = []
    for root in roots:
        files.extend(find_skill_files(root))

    total_problems = 0
    checked = 0
    for path in files:
        checked += 1
        for problem in check_file(path):
            total_problems += 1
            rel = os.path.relpath(path, REPO_ROOT)
            print(f"FAIL: {rel}: {problem}")
            if args.github:
                print(_emit_github(path, problem))

    if total_problems:
        print(f"\n{total_problems} problem(s) across {checked} SKILL.md file(s).")
        return 1
    print(f"PASS: {checked} SKILL.md file(s) have valid frontmatter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
