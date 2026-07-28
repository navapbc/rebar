"""Tests for the commit-message wrap checker (scripts/check_commit_message_wrap.py).

Gerrit renders a change description as preformatted text and preserves the author's
newlines, so wrapping is enforced at commit time rather than left to Gerrit's
warning-only ``commit-message-length-validator``. These tests pin the 50/72 rule AND
the carve-outs — a checker that flagged trailers or URLs would be unusable, because
those lines cannot be wrapped.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CHK_PATH = REPO_ROOT / "scripts" / "check_commit_message_wrap.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_commit_message_wrap", CHK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def chk():
    return _load()


def _check(chk, msg: str) -> list[str]:
    return chk.check_message(msg, subject_limit=50, body_limit=72)


# ---------------------------------------------------------------------------
# Conforming messages must pass
# ---------------------------------------------------------------------------


def test_wrapped_message_passes(chk):
    msg = (
        "a4b2: prune an orphaned local pass-lock ref\n"
        "\n"
        "A local reconcile yielded a held pass lock forever, against a remote\n"
        "that held no lock and with no holder process anywhere.\n"
        "\n"
        "rebar-ticket: a4b2-67bb-1bb0-484f\n"
    )
    assert _check(chk, msg) == []


def test_subject_only_message_passes(chk):
    assert _check(chk, "docs: fix a typo\n") == []


def test_empty_message_is_not_our_error(chk):
    """git aborts an empty commit message itself — don't add a confusing second error."""
    assert _check(chk, "") == []
    assert _check(chk, "\n\n") == []


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


def test_overlong_body_line_is_flagged(chk):
    long_line = (
        "This body line is deliberately far too long to fit inside the seventy-two col limit."
    )
    problems = _check(chk, f"feat: something\n\n{long_line}\n")
    assert len(problems) == 1
    assert f"{len(long_line)} chars" in problems[0]


def test_overlong_subject_is_flagged(chk):
    subject = "feat: a subject line that runs well past the fifty character limit"
    problems = _check(chk, f"{subject}\n\nbody\n")
    assert any("subject is" in p for p in problems)


def test_missing_blank_line_after_subject_is_flagged(chk):
    problems = _check(chk, "feat: thing\nbody starts immediately\n")
    assert any("blank line" in p for p in problems)


def test_line_exactly_at_the_limit_passes(chk):
    """Boundary: 72 is allowed, 73 is not (the limit is inclusive)."""
    assert _check(chk, "feat: x\n\n" + "a" * 72 + "\n") == []
    assert len(_check(chk, "feat: x\n\n" + "a" * 73 + "\n")) == 1


# ---------------------------------------------------------------------------
# Carve-outs — these MUST NOT be flagged
# ---------------------------------------------------------------------------


def test_long_trailer_is_exempt(chk):
    msg = (
        "feat: thing\n"
        "\n"
        "body\n"
        "\n"
        "Signed-off-by: A Very Long Contributor Name <averylongaddress@example.com>\n"
        "Change-Id: I0123456789abcdef0123456789abcdef01234567\n"
        "rebar-ticket: a4b2-67bb-1bb0-484f\n"
    )
    assert _check(chk, msg) == []


def test_long_url_is_exempt(chk):
    msg = (
        "feat: thing\n"
        "\n"
        "See https://example.com/a/very/long/path/that/cannot/be/wrapped/at/all/x.html\n"
    )
    assert _check(chk, msg) == []


def test_fenced_code_block_is_exempt(chk):
    msg = (
        "feat: thing\n"
        "\n"
        "```\n"
        "$ git push gerrit HEAD:refs/for/main --some-really-long-flag=with-a-long-value\n"
        "```\n"
    )
    assert _check(chk, msg) == []


def test_text_after_a_closed_fence_is_still_checked(chk):
    """The fence must TOGGLE, not disable checking for the rest of the message."""
    long_line = "After the fence closes this prose line is much too long to be allowed here."
    msg = f"feat: thing\n\n```\ncode\n```\n{long_line}\n"
    problems = _check(chk, msg)
    assert len(problems) == 1 and "chars" in problems[0]


def test_indented_block_and_table_row_are_exempt(chk):
    msg = (
        "feat: thing\n"
        "\n"
        "    an indented literal block line that is quite long and must stay verbatim\n"
        "| a | table | row | that | is | also | rather | long | and | must | not | wrap |\n"
    )
    assert _check(chk, msg) == []


def test_indent_threshold_is_four_spaces(chk):
    """Boundary: a 4-space/tab indent is a literal block; 1-3 spaces is just prose."""
    body = "x" * 80
    assert _check(chk, f"feat: t\n\n    {body}\n") == []  # 4 spaces -> exempt
    assert _check(chk, f"feat: t\n\n\t{body}\n") == []  # tab -> exempt
    assert len(_check(chk, f"feat: t\n\n   {body}\n")) == 1  # 3 spaces -> checked


def test_long_run_of_ordinary_chars_is_not_atomic(chk):
    """A 73+ char run of plain letters is unwrapped prose, not an unsplittable token."""
    assert len(_check(chk, "feat: t\n\n" + "a" * 90 + "\n")) == 1


def test_bug_tag_is_exempt(chk):
    msg = "feat: thing\n\nBUG=chromium:1234567,chromium:7654321,chromium:1112222,chromium:3334444\n"
    assert _check(chk, msg) == []


# ---------------------------------------------------------------------------
# git's own scaffolding must not produce false positives
# ---------------------------------------------------------------------------


def test_comment_lines_are_ignored(chk):
    msg = (
        "feat: thing\n"
        "\n"
        "body\n"
        "# Please enter the commit message for your changes. Lines starting with '#'\n"
        "# will be ignored, and an empty message aborts the commit. This is long.\n"
    )
    assert _check(chk, msg) == []


def test_verbose_diff_is_ignored(chk):
    """`git commit --verbose` appends a diff whose lines routinely exceed 72."""
    msg = (
        "feat: thing\n"
        "\n"
        "body\n"
        "# ------------------------ >8 ------------------------\n"
        "diff --git a/some/very/long/path/to/a/file.py b/some/very/long/path/to/file.py\n"
        "+    some_long_line_of_added_code_that_is_definitely_longer_than_the_limit = 1\n"
    )
    assert _check(chk, msg) == []


def test_comment_before_subject_does_not_become_the_subject(chk):
    """A leading comment/blank must be skipped when locating the subject line."""
    msg = "# a leading comment git will strip\n\nfeat: thing\n\nbody\n"
    assert _check(chk, msg) == []


# ---------------------------------------------------------------------------
# CLI contract (what the hook actually invokes)
# ---------------------------------------------------------------------------


def test_cli_exit_codes(chk, tmp_path: Path):
    good = tmp_path / "good.txt"
    good.write_text("feat: fine\n\nshort body\n", encoding="utf-8")
    assert chk.main([str(good)]) == 0

    bad = tmp_path / "bad.txt"
    bad.write_text("feat: x\n\n" + "z" * 100 + "\n", encoding="utf-8")
    assert chk.main([str(bad)]) == 1


def test_cli_missing_file_fails_cleanly(chk, tmp_path: Path):
    assert chk.main([str(tmp_path / "nope.txt")]) == 1
