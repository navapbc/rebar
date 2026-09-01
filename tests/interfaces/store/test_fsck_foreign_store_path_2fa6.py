"""fsck reports source paths polluting the tickets tracker (bug 2fa6).

``origin/tickets`` holds nothing but ticket directories and the store's own dotfiles, so
a ``src/`` or ``tests/`` path inside the tracker means something wrote to the store
outside the event-append path — raw git, or a foreign ``git stash`` applied there. The
push recovery now HEALS such a path when it strands the index; this check exists so the
condition is REPORTED too, because healing it silently would hide the writer.

Also pins the false-positive boundary that a first cut got wrong: ticket directories are
NOT required to be id-shaped, so "is this a ticket?" must be the structural
holds-event-files test fsck already uses, not a match on the ticket-id pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar._commands import fsck as _fsck


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def test_clean_store_reports_no_foreign_path(rebar_repo: Path) -> None:
    rebar.create_ticket("task", "clean", repo_root=str(rebar_repo))
    assert _fsck._foreign_store_paths(str(_tracker(rebar_repo))) is None


def test_source_tree_in_the_tracker_is_reported(rebar_repo: Path) -> None:
    rebar.create_ticket("task", "polluted", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    (tracker / "src" / "rebar").mkdir(parents=True)
    (tracker / "src" / "rebar" / "leak.py").write_text("# source\n", encoding="utf-8")

    report = _fsck._foreign_store_paths(str(tracker))
    assert report is not None, "a src/ tree inside the tracker was not reported"
    assert "FOREIGN_STORE_PATH" in report
    assert "src" in report
    # Not committed to the branch — the report must say so rather than over-claiming.
    assert "None are committed" in report


def test_a_ticket_directory_is_never_reported_as_foreign(rebar_repo: Path) -> None:
    """A ticket dir whose name is not id-shaped is still ticket data.

    RED on the first implementation, which required the name to match the ticket-id
    pattern and therefore reported healthy stores as polluted."""
    tracker = _tracker(rebar_repo)
    odd = tracker / "human-readable-ticket-name"
    odd.mkdir()
    (odd / "1700000000000000000-aaaa-CREATE.json").write_text("{}", encoding="utf-8")

    report = _fsck._foreign_store_paths(str(tracker))
    assert report is None, f"a ticket directory was misreported as pollution: {report}"


def test_empty_shard_container_is_never_reported_as_foreign(rebar_repo: Path) -> None:
    """Rollback/failed-create cleanup may leave an empty two-hex shard container."""
    tracker = _tracker(rebar_repo)
    (tracker / "ab").mkdir()

    report = _fsck._foreign_store_paths(str(tracker))
    assert report is None, f"an empty shard container was misreported as pollution: {report}"


def test_two_hex_pollution_with_non_ticket_child_is_reported(rebar_repo: Path) -> None:
    """A two-hex name is not enough; shard contents must still be ticket data."""
    tracker = _tracker(rebar_repo)
    (tracker / "ab" / "src").mkdir(parents=True)
    (tracker / "ab" / "src" / "leak.py").write_text("# source\n", encoding="utf-8")

    report = _fsck._foreign_store_paths(str(tracker))
    assert report is not None, "a polluted shard container was not reported"
    assert "FOREIGN_STORE_PATH" in report
    assert "ab" in report


def test_fsck_counts_the_foreign_path_as_an_issue(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rebar.create_ticket("task", "counted", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    (tracker / "tests").mkdir()
    (tracker / "tests" / "test_leak.py").write_text("# source\n", encoding="utf-8")

    rc = _fsck.fsck_cli([], repo_root=str(rebar_repo))
    out = capsys.readouterr().out
    assert "FOREIGN_STORE_PATH" in out, out
    assert rc == 1, "pollution must be a COUNTED issue (non-zero exit), not a bare WARN"
