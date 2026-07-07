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
    # A valid short handle: non-empty, not the raw full id, and it resolves back to
    # this exact ticket THROUGH the symlinked tracker. Asserting a fixed char-length
    # bound (`len(out) <= 32`) was brittle: `auto` returns the adjective-adjective-animal
    # alias, whose length is the sum of wordlist word lengths (no hard cap), so a long
    # alias tripped it in CI (bug 9b3a / subsequent-glandular-albino). Round-trip
    # resolution is the true invariant of symlink resolution and is length-independent.
    assert out, f"format {mode} via symlink returned empty (the -L regression)"
    assert out != full_id
    resolved, rrc = _format(["resolve", out], capsys)
    assert rrc == 0, f"resolve {out!r} via symlink failed (rc={rrc})"
    assert resolved == full_id, f"format {mode} -> {out!r} did not resolve back to {full_id!r}"


def test_format_auto_alias_length_is_not_bounded(
    rebar_repo: Path,
    symlinked_tracker: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`format auto` returns the ticket's alias, whose length is wordlist-dependent and
    intentionally NOT capped. Guards against re-introducing a brittle ``len(out) <= N``
    bound (bug 9b3a): a long alias — the failure mode that produced ``assert 35 <= 32`` in
    CI — must still resolve back to its ticket through the symlinked tracker.
    """
    long_alias = "extraordinarily-unbecoming-hippopotamus"  # 39 chars: exceeds any fixed bound
    monkeypatch.setattr("rebar._alias.compute_genesis_alias", lambda _tid: long_alias)
    full_id = rebar.create_ticket("task", "long alias", repo_root=str(rebar_repo))

    out, rc = _format(["format", full_id, "auto"], capsys)
    assert rc == 0
    assert out == long_alias and len(out) > 32  # the retired `len(out) <= 32` bound would fail here
    resolved, rrc = _format(["resolve", out], capsys)
    assert rrc == 0 and resolved == full_id  # but the true invariant (round-trip) still holds
