"""Tests for the SKILL.md frontmatter lint (ticket db04).

The checker (scripts/check_skill_frontmatter.py) keeps Agent Skills SKILL.md files
loadable: GitHub Copilot CLI silently drops a skill whose frontmatter fails to
parse or whose ``description`` exceeds 1024 characters, so this gate makes that
failure class loud at commit/CI time. The rules mirror Anthropic's reference
validator (anthropics/skills:.../quick_validate.py).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CHK_PATH = REPO_ROOT / "scripts" / "check_skill_frontmatter.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_skill_frontmatter", CHK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chk = _load()


def _write_skill(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    p = d / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return p


# ─────────────────────────── HAPPY PATH (shown to implementer) ────────────────


def test_real_tree_passes():
    """The committed examples/agent-skills tree passes (exit 0)."""
    assert chk.main([]) == 0


def test_clean_synthetic_skill_has_no_problems(tmp_path: Path):
    p = _write_skill(
        tmp_path,
        "good-skill",
        "---\nname: good-skill\ndescription: A short, valid description.\n---\n\n# Body\n",
    )
    assert chk.check_file(p) == []


def test_name_is_optional(tmp_path: Path):
    """Copilot CLI derives the name from the directory; a nameless skill is valid."""
    p = _write_skill(
        tmp_path,
        "nameless",
        "---\ndescription: Valid description with no name field.\n---\n",
    )
    assert chk.check_file(p) == []


def test_block_scalar_description_is_valid(tmp_path: Path):
    """A folded block scalar with a colon inside parses fine (the debug fix)."""
    p = _write_skill(
        tmp_path,
        "block",
        "---\nname: block\ndescription: >-\n  Enforces RED then GREEN discipline: write a "
        "failing test first, then fix it.\n---\n",
    )
    assert chk.check_file(p) == []


# ─────────────────────────── FAILURE / EDGE PATHS (held out) ──────────────────


def test_description_over_cap_fails(tmp_path: Path):
    long_desc = "x" * (chk.DESCRIPTION_MAX_LENGTH + 1)
    p = _write_skill(tmp_path, "toolong", f"---\nname: toolong\ndescription: {long_desc}\n---\n")
    problems = chk.check_file(p)
    assert any("maximum is 1024" in m for m in problems)


def test_description_at_cap_passes(tmp_path: Path):
    """Boundary: exactly 1024 characters is allowed."""
    desc = "y" * chk.DESCRIPTION_MAX_LENGTH
    p = _write_skill(tmp_path, "atcap", f"---\nname: atcap\ndescription: {desc}\n---\n")
    assert chk.check_file(p) == []


def test_empty_description_fails(tmp_path: Path):
    p = _write_skill(tmp_path, "empty", '---\nname: empty\ndescription: ""\n---\n')
    problems = chk.check_file(p)
    assert any("non-empty" in m for m in problems)


def test_missing_description_fails(tmp_path: Path):
    p = _write_skill(tmp_path, "nodesc", "---\nname: nodesc\n---\n")
    problems = chk.check_file(p)
    assert any("missing required 'description'" in m for m in problems)


def test_unquoted_colon_breaks_yaml_fails(tmp_path: Path):
    """An unquoted ': ' turns the plain scalar into a mapping / parse error."""
    p = _write_skill(
        tmp_path,
        "colon",
        "---\nname: colon\ndescription: RED then GREEN discipline: write a failing test\n---\n",
    )
    problems = chk.check_file(p)
    assert problems  # non-empty: either a parse error or a non-string description


def test_angle_brackets_in_description_fails(tmp_path: Path):
    p = _write_skill(
        tmp_path,
        "angle",
        "---\nname: angle\ndescription: Wraps output in <tags> for the model.\n---\n",
    )
    problems = chk.check_file(p)
    assert any("angle bracket" in m for m in problems)


def test_name_over_64_chars_fails(tmp_path: Path):
    long_name = "a" * (chk.NAME_MAX_LENGTH + 1)
    p = _write_skill(tmp_path, "longname", f"---\nname: {long_name}\ndescription: ok.\n---\n")
    problems = chk.check_file(p)
    assert any("maximum is 64" in m for m in problems)


def test_name_bad_charset_fails(tmp_path: Path):
    p = _write_skill(tmp_path, "badname", "---\nname: Bad_Name\ndescription: ok.\n---\n")
    problems = chk.check_file(p)
    assert any("kebab-case" in m for m in problems)


def test_missing_frontmatter_fails(tmp_path: Path):
    p = _write_skill(tmp_path, "nofm", "# Just a heading, no frontmatter\n")
    problems = chk.check_file(p)
    assert any("frontmatter" in m for m in problems)


def test_main_returns_nonzero_on_bad_file(tmp_path: Path):
    long_desc = "z" * (chk.DESCRIPTION_MAX_LENGTH + 1)
    p = _write_skill(tmp_path, "bad", f"---\nname: bad\ndescription: {long_desc}\n---\n")
    assert chk.main([str(p)]) == 1


def test_main_scans_directory(tmp_path: Path):
    _write_skill(tmp_path, "ok1", "---\nname: ok1\ndescription: fine.\n---\n")
    long_desc = "q" * (chk.DESCRIPTION_MAX_LENGTH + 1)
    _write_skill(tmp_path, "bad2", f"---\nname: bad2\ndescription: {long_desc}\n---\n")
    assert chk.main([str(tmp_path)]) == 1
