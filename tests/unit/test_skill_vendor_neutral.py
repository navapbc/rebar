"""Vendor-neutrality gate for the tidying and defect-audit skills (ticket 6b48).

rebar must support a wide variety of providers and models, so a prompt-shaped
skill must not assume a vendor's model, tool-calling syntax or harness. Both
skill trees are scanned for the provider tokens ``src/rebar/llm/providers.py``
registers plus the model families those providers serve; any match fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "examples" / "agent-skills"
SKILL_TREES = ("rebar-tidy", "rebar-janitor")

# Provider ids registered in src/rebar/llm/providers.py, plus the model families
# they serve. Longest-first so a compound id is reported rather than its prefix.
DENYLIST = (
    "openai-responses",
    "google-vertex",
    "openai-chat",
    "google-gla",
    "anthropic",
    "vertexai",
    "bedrock",
    "claude",
    "gemini",
    "openai",
    "gpt",
)

_DENY_RE = re.compile("|".join(re.escape(token) for token in DENYLIST), re.IGNORECASE)


def _skill_files(tree: str) -> list[Path]:
    root = SKILLS_DIR / tree
    return sorted(p for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("tree", SKILL_TREES)
def test_skill_tree_exists(tree: str) -> None:
    assert (SKILLS_DIR / tree / "SKILL.md").is_file(), f"missing skill: {tree}/SKILL.md"


@pytest.mark.parametrize("tree", SKILL_TREES)
def test_skill_tree_names_no_vendor_or_model(tree: str) -> None:
    hits: list[str] = []
    for path in _skill_files(tree):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _DENY_RE.search(line)
            if match is not None:
                rel = path.relative_to(REPO_ROOT)
                hits.append(f"{rel}:{lineno}: {match.group(0)!r} in {line.strip()!r}")
    assert not hits, "skill names a specific vendor or model:\n" + "\n".join(hits)
