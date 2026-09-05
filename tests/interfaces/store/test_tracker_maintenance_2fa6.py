"""The supported maintenance door and its safety envelope (bug 2fa6).

The command's value is the envelope, not the repair, so these tests pin the envelope:

* the backup ref exists BEFORE the first write (the predecessor tagged mid-run, which made
  the tag useless for rollback — the specific mistake being designed out);
* a refusal when unpushed ticket commits are present, because that is the one condition
  separating a recoverable local mess from real event loss — and a refused run must make
  NO writes at all, backup ref included;
* the refusal fails CLOSED when it cannot prove the local commits are safe;
* the break-glass demands a written reason and is recorded as such.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

from rebar._commands import tracker_maintenance as _tm


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _commit(cwd: Path, msg: str) -> None:
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-q", "-m", msg)


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    """A tickets tracker with an origin, polluted by a source tree."""
    remote = tmp_path / "remote.git"
    init_bare_remote(remote, initial_branch="tickets")
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "--quiet", str(remote), str(seed))
    tdir = seed / "aaaa-bbbb-cccc-dddd"
    tdir.mkdir()
    (tdir / "1700000000000000000-u1-CREATE.json").write_text("{}", encoding="utf-8")
    _commit(seed, "seed")
    _git(seed, "push", "--quiet", "origin", "HEAD:tickets")

    repo = tmp_path / "repo"
    repo.mkdir()
    tracker = repo / ".tickets-tracker"
    _git(tmp_path, "clone", "--quiet", "-b", "tickets", str(remote), str(tracker))
    _git(tracker, "fetch", "--quiet", "origin")
    # Pollution: a source tree that cannot be ticket data.
    (tracker / "src").mkdir()
    (tracker / "src" / "leak.py").write_text("# source\n", encoding="utf-8")
    return tracker


def _backup_refs(tracker: Path) -> list[str]:
    out = _git(tracker, "for-each-ref", "--format=%(refname)", "refs/rebar-maintenance/")
    return [r for r in out.stdout.split() if r.strip()]


def _audit_lines(tracker: Path) -> list[dict]:
    path = _tm._audit_path(str(tracker))
    if path is None or not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_status_reports_without_writing(tracker: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = _tm.tracker_maintenance_cli(["--status"], repo_root=str(tracker.parent))
    out = capsys.readouterr().out
    assert rc == 0
    assert "foreign top-level paths : 1" in out
    assert (tracker / "src" / "leak.py").exists(), "--status must not repair"
    assert _backup_refs(tracker) == [], "--status must not create a backup ref"


def test_clean_takes_the_backup_ref_then_repairs(
    tracker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head_before = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    rc = _tm.tracker_maintenance_cli(["--clean"], repo_root=str(tracker.parent))
    out = capsys.readouterr().out
    assert rc == 0, out

    refs = _backup_refs(tracker)
    assert len(refs) == 1, f"expected exactly one backup ref, got {refs}"
    # The rollback point must name the state BEFORE the run, not after it.
    assert _git(tracker, "rev-parse", refs[0]).stdout.strip() == head_before
    assert "backup ref:" in out and "rollback:" in out

    assert not (tracker / "src").exists(), "the foreign source tree was not removed"
    audit = _audit_lines(tracker)
    assert len(audit) == 1, audit
    assert audit[0]["forced"] is False
    assert audit[0]["backup_ref"] == refs[0]
    assert audit[0]["foreign_paths"] == ["src"]


def test_unpushed_commits_refuse_and_write_nothing(
    tracker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal must be total: no repair AND no backup ref."""
    extra = tracker / "eeee-ffff-0000-1111"
    extra.mkdir()
    (extra / "1700000000000000001-u2-CREATE.json").write_text("{}", encoding="utf-8")
    _commit(tracker, "ticket: CREATE eeee-ffff-0000-1111")
    assert _tm._unpushed_commits(str(tracker)) == 1

    rc = _tm.tracker_maintenance_cli(["--clean"], repo_root=str(tracker.parent))
    err = capsys.readouterr().err
    assert rc == 1
    assert "Refusing to repair" in err
    assert (tracker / "src" / "leak.py").exists(), "a refused run must not repair"
    assert _backup_refs(tracker) == [], "a refused run must not write a backup ref"
    assert _audit_lines(tracker) == []


def test_break_glass_overrides_the_refusal_and_is_recorded(
    tracker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extra = tracker / "eeee-ffff-0000-1111"
    extra.mkdir()
    (extra / "1700000000000000001-u2-CREATE.json").write_text("{}", encoding="utf-8")
    _commit(tracker, "ticket: CREATE eeee-ffff-0000-1111")

    rc = _tm.tracker_maintenance_cli(
        ["--clean", "--force=remote is gone; operator accepts the risk"],
        repo_root=str(tracker.parent),
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "BREAK-GLASS" in captured.err, "the override must be loud"
    assert "operator accepts the risk" in captured.err

    audit = _audit_lines(tracker)
    assert len(audit) == 1
    assert audit[0]["forced"] is True
    assert audit[0]["force_reason"] == "remote is gone; operator accepts the risk"
    assert audit[0]["unpushed_commits"] == 1


def test_force_without_a_reason_is_rejected(
    tracker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _tm.tracker_maintenance_cli(["--clean", "--force"], repo_root=str(tracker.parent))
    assert rc == 2
    assert "requires a written reason" in capsys.readouterr().err


def test_unknowable_unpushed_state_fails_closed(
    tracker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No origin/tickets means local commits cannot be proven safe — refuse, don't assume 0."""
    _git(tracker, "update-ref", "-d", "refs/remotes/origin/tickets")
    assert _tm._unpushed_commits(str(tracker)) is None

    rc = _tm.tracker_maintenance_cli(["--clean"], repo_root=str(tracker.parent))
    assert rc == 1
    assert "Refusing to repair" in capsys.readouterr().err
    assert _backup_refs(tracker) == []
