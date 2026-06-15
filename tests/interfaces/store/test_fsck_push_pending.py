"""fsck surfaces PUSH_PENDING when the local tickets branch is ahead of origin.

In-process port of tests/scripts/test-ticket-fsck-push-pending.sh (the bash engine
is being deleted). Push is best-effort, so a local commit with no push silently
diverges from origin; fsck must surface that (it is informational — it does NOT
fail the fsck). Drives ``rebar._cli.main(["fsck"])`` against a tracker ahead of a
real local bare origin.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar
from rebar import _cli


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_q(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def repo_with_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path]]:
    """Initialized rebar repo wired to a real local bare origin; origin/tickets
    seeded so divergence is observable. Yields (repo, tracker)."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t.co", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    tracker = repo / ".tickets-tracker"
    # Seed origin/tickets (REBAR_PUSH=always) so a later un-pushed commit diverges.
    monkeypatch.setenv("REBAR_PUSH", "always")
    rebar.create_ticket("task", "seed", repo_root=str(repo))
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    yield repo, tracker


def _ahead(tracker: Path) -> int:
    cp = _git_q("rev-list", "origin/tickets..HEAD", "--count", cwd=tracker)
    return int((cp.stdout or "0").strip() or "0")


def test_fsck_reports_push_pending_and_stays_exit_0(
    repo_with_origin: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, tracker = repo_with_origin
    # A local-only commit: push off so origin does not advance.
    monkeypatch.setenv("REBAR_PUSH", "off")
    rebar.create_ticket("task", "unpushed local ticket", repo_root=str(repo))
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    assert _ahead(tracker) >= 1, "fixture did not reach a local-ahead state"

    rc = _cli.main(["fsck"])
    out = capsys.readouterr().out

    assert "PUSH_PENDING" in out, f"fsck did not surface PUSH_PENDING; output:\n{out}"
    # Informational only — must not turn a clean fsck into a failure.
    assert rc == 0, f"PUSH_PENDING should not be an integrity failure (exit {rc})"


def test_fsck_quiet_when_in_sync(
    repo_with_origin: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, tracker = repo_with_origin
    # Push HEAD to origin so local and origin/tickets are level.
    _git_q("push", "origin", "HEAD:tickets", cwd=tracker)
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    assert _ahead(tracker) == 0, "fixture unexpectedly ahead of origin"

    _cli.main(["fsck"])
    out = capsys.readouterr().out
    assert "PUSH_PENDING" not in out, f"fsck emitted PUSH_PENDING when in sync:\n{out}"
