"""Snapshot builds keep the path git writes into short (bug 09f9-6b9f-24b1-4fb6).

git for Windows refuses to create a file whose path exceeds MAX_PATH (259 usable characters)
unless ``core.longpaths`` is set. A ticket snapshot used to be checked out in the store's tmp
directory under ``tickets-<sha12>-<uuid32>/.tickets-tracker`` (75 characters of scaffolding),
which pushed real event files past the limit on the Windows sweep. git now writes into a flat
``t-<8 hex>`` there, and Python renames the result into the published layout.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from rebar._snapshot import repo_snapshot as rs

pytestmark = pytest.mark.unit

# MAX_PATH less the terminating NUL.
_MAX_PATH = 259
# Longest gate root measured on the Windows sweep (bug 09f9-6b9f-24b1-4fb6).
_CI_GATE_ROOT = 123
# The rename-only published layout is read by Python, which needs LongPathsEnabled beyond this.
_PUBLISHED_GATE_ROOT = 63
_TICKET_DIR = len("b8d8-19cf-1fcb-4a46")
_EVENT_FILE = len("1794837868190575901-337d58f4-78c4-487a-9cff-8a6e490e4872-REVIEW_RESULT.json")
# Longest tracker-relative path in the real store: <ticket>/<event>-COMPLETION_VERDICT.json.retired
_LONGEST_RELATIVE = 108
_STORE_DIR = len("/rebar-gate-snapshots")
_PUBLISHED = len("/tickets-") + 40 + len("/.tickets-tracker/")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def _isolate_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = tmp_path / "gate-tmpdir"
    store.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(store))


@pytest.fixture
def repo(tmp_path: Path, _isolate_store: None) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("init", "--quiet"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "Test"),
        ("config", "commit.gpgsign", "false"),
    ):
        _git(r, *args)
    (r / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "--quiet", "-m", "seed")
    _git(r, "checkout", "--quiet", "-b", "tickets")
    ticket = r / "b8d8-19cf-1fcb-4a46"
    ticket.mkdir()
    (ticket / "1794837868190575901-337d58f4-COMMENT.json").write_text(
        '{"x": 1}\n', encoding="utf-8"
    )
    _git(r, "add", "-A")
    _git(r, "commit", "--quiet", "-m", "tickets")
    return r


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every directory git is asked to write a tree into."""
    seen: list[Path] = []
    real = rs._materialize_tree

    def spy(repo_root: str, sha: str, dest_tmp: Path) -> None:
        seen.append(dest_tmp)
        real(repo_root, sha, dest_tmp)

    monkeypatch.setattr(rs, "_materialize_tree", spy)
    return seen


def test_git_writes_a_ticket_snapshot_into_a_short_flat_build(
    repo: Path, written: list[Path]
) -> None:
    store = rs.store_root()

    root = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))

    assert len(written) == 1
    build = written[0]
    assert build.parent == store / "tmp"
    assert re.fullmatch(r"t-[0-9a-f]{8}", build.name), build.name
    assert len(str(build)) - len(str(store)) == len("/tmp/t-") + 8
    tracker = root / ".tickets-tracker"
    on_disk = sorted(p.relative_to(tracker).as_posix() for p in tracker.rglob("*") if p.is_file())
    sha = root.name.removeprefix("tickets-")
    assert on_disk == sorted(_git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())
    assert list((store / "tmp").iterdir()) == []


def test_git_writes_a_code_snapshot_into_a_short_build(repo: Path, written: list[Path]) -> None:
    rs.materialize("HEAD", repo_root=str(repo), fetch=False)

    assert len(written) == 1
    assert written[0].parent == rs.store_root() / "tmp"
    assert re.fullmatch(r"c-[0-9a-f]{8}", written[0].name), written[0].name


def test_the_worst_measured_paths_fit_max_path() -> None:  # pure arithmetic, no store
    scaffolding = len("/tmp/t-") + 8
    git_written = _CI_GATE_ROOT + _STORE_DIR + scaffolding + 1 + _TICKET_DIR + 1 + _EVENT_FILE
    assert git_written <= _MAX_PATH, git_written
    published = _PUBLISHED_GATE_ROOT + _STORE_DIR + _PUBLISHED + _LONGEST_RELATIVE
    assert published <= _MAX_PATH, published


def test_a_failed_build_leaves_nothing_in_tmp(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(repo_root: str, sha: str, dest_tmp: Path) -> None:
        dest_tmp.mkdir(parents=True, exist_ok=True)
        (dest_tmp.parent / (dest_tmp.name + ".index")).write_text("idx", encoding="utf-8")
        raise rs.SnapshotError("simulated checkout failure")

    monkeypatch.setattr(rs, "_materialize_tree", boom)

    with pytest.raises(rs.SnapshotError, match="simulated"):
        rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False)

    assert list((rs.store_root() / "tmp").iterdir()) == []


def test_losing_the_publish_race_returns_the_winner(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rs._materialize_tree
    sha = _git(repo, "rev-parse", "tickets")

    def win_race_first(repo_root: str, sha_: str, dest_tmp: Path) -> None:
        real(repo_root, sha_, dest_tmp)
        winner = rs.store_root() / f"tickets-{sha}" / ".tickets-tracker"
        winner.mkdir(parents=True)
        (winner / "marker").write_text("theirs", encoding="utf-8")

    monkeypatch.setattr(rs, "_materialize_tree", win_race_first)

    root = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))

    assert root == rs.store_root() / f"tickets-{sha}"
    assert (root / ".tickets-tracker" / "marker").read_text(encoding="utf-8") == "theirs"
    assert list((rs.store_root() / "tmp").iterdir()) == []
