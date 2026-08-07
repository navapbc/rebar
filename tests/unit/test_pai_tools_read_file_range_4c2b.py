"""``read_file`` must not answer a malformed range by silently reading to EOF (bug 4c2b).

``read_file`` picks its upper bound with
``hi = line_end if line_end and line_end >= lo else len(lines)``. That ``else`` is the
tool's documented "read to EOF" fallback for the ``line_end == 0`` sentinel — but the
guard selecting it is also false for an **inverted** range (``0 < line_end < line_start``,
or a negative ``line_end``). Both land on ``len(lines)``, so an agent that asks for a
bounded window whose end precedes its start is handed the whole rest of the file, capped
at ``_READ_MAX_LINES``, with nothing in the return value saying so.

Observed live: the looping completion-verifier in bug bf31 issued
``read_file(<path>, line_start=115, line_end=30)`` **31 times**, each returning the same
~11,000-character blob for a request it had framed as a 30-line window.

This is the class bug ``bf31-fd55-d28d-4b7a`` fixed in the sibling tool -- ``search_files``
must not fake "(no matches)" -- and the contract is stated in ``read_file``'s own neighbour at
``pai_tools.py``: an error is never reported as an ordinary answer. These are the
read-only tools every rebar LLM gate hands its agent, and those gates SIGN their verdicts,
so a tool that answers a request it did not perform is a false-verdict vector.

The contract these tests pin, in three parts:

* an inverted range is REPORTED, not substituted;
* ``line_end == 0`` still means "read to EOF, capped" — the documented sentinel is
  untouched (pinned as an explicit regression, since it shares the branch being changed);
* an ordinary ``line_start <= line_end`` window is unchanged.
"""

from __future__ import annotations

import pytest

from rebar.llm import pai_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def tree(tmp_path):
    """A file long enough that "read to EOF" and any bounded window are unmistakably
    different lengths, with per-line content that identifies the line — so no assertion
    can pass by accident off a single hardcoded field."""
    (tmp_path / "sample.txt").write_text(
        "".join(f"content-of-line-{i}\n" for i in range(1, 61)), encoding="utf-8"
    )
    (tmp_path / "short.txt").write_text("only\ntwo\n", encoding="utf-8")
    return tmp_path


def _read_file(root):
    """The real tool, built exactly as the runner builds it (``pai_tools.py``)."""
    read_file, _list_directory, _search_files = pai_tools.filesystem_tools(str(root))
    return read_file


# ── the defect: an inverted range must be reported, not silently widened ──────


@pytest.mark.parametrize(
    ("line_start", "line_end"),
    [
        (115, 30),  # the shape observed live in bf31
        (15, 8),  # both ends inside the file
        (2, 1),  # off by exactly one
        (10, -5),  # negative end — also below the start, also malformed
    ],
)
def test_inverted_range_is_reported_not_read_to_eof(tree, line_start, line_end):
    out = _read_file(tree)("sample.txt", line_start, line_end)

    assert out.startswith("Error:"), (
        f"read_file(line_start={line_start}, line_end={line_end}) must report the "
        f"malformed range; got {len(out.splitlines())} line(s) of content instead: {out[:200]!r}"
    )
    # The message must name the range, so the agent can correct its own call rather than
    # guess. Both bounds appear verbatim.
    assert str(line_start) in out and str(line_end) in out, (
        f"the error must name the malformed range it rejected; got {out!r}"
    )
    # And it must not have leaked file content under the error.
    assert "content-of-line-" not in out, f"content leaked into the error: {out!r}"


def test_inverted_range_does_not_depend_on_file_length(tree):
    """The rejection is a property of the request, not of the file: a range inverted past
    the end of a 2-line file is reported the same way."""
    out = _read_file(tree)("short.txt", 9, 4)
    assert out.startswith("Error:"), out
    assert "only" not in out and "two" not in out, out


# ── the documented sentinel and the ordinary window must be unchanged ─────────


def test_line_end_zero_still_reads_to_eof_capped(tree):
    """``line_end=0`` is a documented part of the tool's contract and shares the branch
    being changed — pin it."""
    out = _read_file(tree)("sample.txt", 58, 0)
    assert out.splitlines() == [
        "58\tcontent-of-line-58",
        "59\tcontent-of-line-59",
        "60\tcontent-of-line-60",
    ], out


def test_line_end_zero_from_the_top_reads_the_whole_file(tree):
    out = _read_file(tree)("sample.txt", 1, 0)
    lines = out.splitlines()
    assert len(lines) == 60, out[:200]
    assert lines[0] == "1\tcontent-of-line-1"
    assert lines[-1] == "60\tcontent-of-line-60"


def test_ordinary_window_is_unchanged(tree):
    out = _read_file(tree)("sample.txt", 12, 15)
    assert out.splitlines() == [
        "12\tcontent-of-line-12",
        "13\tcontent-of-line-13",
        "14\tcontent-of-line-14",
        "15\tcontent-of-line-15",
    ], out


def test_single_line_window_is_unchanged(tree):
    """``line_start == line_end`` is the boundary next to the rejected region and must
    still be a legal one-line read."""
    out = _read_file(tree)("sample.txt", 7, 7)
    assert out.splitlines() == ["7\tcontent-of-line-7"], out
