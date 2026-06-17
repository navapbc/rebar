"""``format <id>`` must resolve through a SYMLINKED tracker.

In-process port of tests/scripts/test-format-ticket-id-symlink.sh (bug
3203-236a-09bc-44f4: the bash fallback used ``find`` without ``-L``, silently
returning zero results when ``.tickets-tracker`` is reached via a symlink — common
in worktree sessions where ``REBAR_TRACKER_DIR`` points at a symlink). The
in-process resolver walks with ``os.listdir``/``os.path.isdir`` (which follow
symlinks), so both ``short`` and ``auto`` must return a valid short form.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import _cli


def _format(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[str, int]:
    capsys.readouterr()
    rc = _cli.main(argv)
    return capsys.readouterr().out.strip(), rc


@pytest.fixture
def symlinked_tracker(rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A symlink pointing at the repo's real tracker; sets REBAR_TRACKER_DIR to it."""
    link = tmp_path / "tracker-link"
    link.symlink_to(rebar_repo / ".tickets-tracker")
    assert link.is_symlink() and link.is_dir()
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(link))
    return str(link)


@pytest.mark.parametrize("mode", ["short", "auto"])
def test_format_via_symlink_tracker(
    rebar_repo: Path,
    symlinked_tracker: str,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    full_id = rebar.create_ticket("task", f"symlink test {mode}", repo_root=str(rebar_repo))
    out, rc = _format(["format", full_id, mode], capsys)
    assert rc == 0
    # A valid short form: non-empty, not the raw full id, within the bash bound.
    assert out, f"format {mode} via symlink returned empty (the -L regression)"
    assert out != full_id
    assert len(out) <= 32
