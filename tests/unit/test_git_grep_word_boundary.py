"""Bug 743a-96fa-fdc8-4f98: ``\\b`` under ``git grep -E`` is not a word boundary on macOS.

macOS git compiles ``-E`` patterns so that ``alpha\\b`` means ``alphab``: against the lines
``alpha beta`` and ``alphabeta`` it selects ``alphabeta``. Linux git handles ``\\b``. The plan
guide therefore tells authors to write removal-verification greps in a portable form.

This module pins both halves. The portable forms must select the right line on every platform.
The trap itself is reproduced only on darwin, so a future macOS git that fixes it fails here and
prompts a revisit of the guidance. ``tests/conftest.py`` lists this module in
``_PLATFORM_COMPAT_PATH_FRAGMENTS`` so the macOS CI cell, which runs only ``platform_compat``
tests, executes the darwin-only reproduction.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from rebar.llm.plan_review.registry import explain_guide

pytestmark = pytest.mark.unit

WORD_LINE = "alpha beta"
TRAP_LINE = "alphabeta"
PORTABLE_ERE = "(^|[^A-Za-z0-9_])alpha([^A-Za-z0-9_]|$)"


@pytest.fixture
def fixture_file(tmp_path: Path) -> Path:
    path = tmp_path / "words.txt"
    path.write_text(f"{WORD_LINE}\n{TRAP_LINE}\n", encoding="utf-8")
    return path


def _git_grep(fixture_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "grep", "--no-index", "-h", *args, "--", fixture_file.name],
        cwd=fixture_file.parent,
        capture_output=True,
        text=True,
        check=False,
    )


def _matched_lines(fixture_file: Path, *args: str) -> list[str]:
    result = _git_grep(fixture_file, *args)
    # 0 = matched, 1 = no match; anything else is an error that must not read as "0 hits".
    assert result.returncode in (0, 1), (result.returncode, result.stderr)
    return result.stdout.splitlines()


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("-E", PORTABLE_ERE), id="ere-explicit-classes"),
        pytest.param(("-w", "alpha"), id="word-regexp"),
        pytest.param(("-Fw", "alpha"), id="fixed-word"),
    ],
)
def test_portable_word_boundary_forms_select_only_the_word(
    fixture_file: Path, args: tuple[str, ...]
) -> None:
    assert _matched_lines(fixture_file, *args) == [WORD_LINE]


def test_pcre_word_boundary_selects_only_the_word(fixture_file: Path) -> None:
    result = _git_grep(fixture_file, "-P", r"alpha\b")
    if result.returncode not in (0, 1):
        pytest.skip(f"git grep -P unavailable in this git build: {result.stderr.strip()}")
    assert result.stdout.splitlines() == [WORD_LINE]


@pytest.mark.skipif(sys.platform != "darwin", reason="the -E \\b trap is macOS-only")
def test_macos_ere_reads_backslash_b_as_literal_b(fixture_file: Path) -> None:
    assert _matched_lines(fixture_file, "-E", r"alpha\b") == [TRAP_LINE]


def test_plan_guide_names_the_trap_and_portable_forms() -> None:
    text = explain_guide("plan")
    assert "`git grep -E` is not a word boundary on macOS" in text
    assert "git grep -nE 'alpha\\b'" in text
    assert PORTABLE_ERE.replace("alpha", "symbol") in text
    assert "git grep -nw 'symbol'" in text
    assert "git grep -nP 'symbol\\b'" in text
    assert "git grep -nF" in text
