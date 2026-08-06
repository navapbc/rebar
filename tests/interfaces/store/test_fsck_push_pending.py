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
    rebar.init_repo(repo_root=str(repo))

    tracker = repo / ".tickets-tracker"
    # Seed origin/tickets (REBAR_SYNC_PUSH=always) so a later un-pushed commit diverges.
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
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
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
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
    _repo, tracker = repo_with_origin
    # Push HEAD to origin so local and origin/tickets are level.
    _git_q("push", "origin", "HEAD:tickets", cwd=tracker)
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    assert _ahead(tracker) == 0, "fixture unexpectedly ahead of origin"

    _cli.main(["fsck"])
    out = capsys.readouterr().out
    assert "PUSH_PENDING" not in out, f"fsck emitted PUSH_PENDING when in sync:\n{out}"


# ── DIVERGED: a store that cannot fast-forward onto origin/tickets (bug 01e8) ─────────
#
# When init could not see origin/tickets (a single-branch clone) it built a fresh orphan
# store; _store/sync.py refuses to absorb an orphan that carries events and LOGS a
# best-effort "DIVERGED" warning, but that warning is invisible to an operator who runs
# the dedicated health check. fsck must surface divergence as a REAL, counted issue —
# not mislabel it as the informational PUSH_PENDING "ahead by N" (which tells the
# operator to just retry a write, when in fact the store will never fast-forward).


def test_fsck_reports_diverged_on_unrelated_history(
    repo_with_origin: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo, tracker = repo_with_origin
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    # Rewrite the local tracker onto an orphan branch that shares NO history with
    # origin/tickets — exactly the divergent empty/independent store bug 01e8 produced.
    _git("checkout", "-q", "--orphan", "diverged-orphan", cwd=tracker)
    _git_q("rm", "-rfq", ".", cwd=tracker)
    (tracker / "unrelated.txt").write_text("independently built store\n", encoding="utf-8")
    _git("add", "-A", cwd=tracker)
    _git("commit", "-qm", "orphan store root (no common ancestor with origin)", cwd=tracker)
    _git("branch", "-qM", "tickets", cwd=tracker)
    assert _git_q("merge-base", "HEAD", "origin/tickets", cwd=tracker).returncode != 0, (
        "precondition: local and origin/tickets must share no common ancestor"
    )

    rc = _cli.main(["fsck"])
    out = capsys.readouterr().out

    assert "DIVERGED" in out, f"fsck did not surface DIVERGED on an unrelated history:\n{out}"
    assert "PUSH_PENDING" not in out, f"fsck mislabeled a divergent store as push-pending:\n{out}"
    assert rc == 1, f"DIVERGED must be a counted integrity issue (non-zero exit); got {rc}"


def test_fsck_reports_diverged_on_non_fast_forward(
    repo_with_origin: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar._commands import fsck

    _repo, tracker = repo_with_origin
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    # Advance origin/tickets by one commit the local tracker never sees, then make a
    # divergent local commit atop the shared base: common ancestor exists, but neither
    # side is an ancestor of the other → a non-fast-forwardable divergence. Assert the
    # classifier directly (the full CLI would trigger a best-effort reconverge that
    # union-merges a common-ancestor divergence, which is correct auto-healing and would
    # mask the transient state this check reports when a fetch cannot heal it, e.g.
    # offline).
    base = _git_q("rev-parse", "HEAD", cwd=tracker).stdout.strip()
    _git("checkout", "-q", "-b", "remote-advance", cwd=tracker)
    (tracker / "remote-only.txt").write_text("origin-side commit\n", encoding="utf-8")
    _git("add", "-A", cwd=tracker)
    _git("commit", "-qm", "origin-side divergent commit", cwd=tracker)
    _git_q("push", "-f", "origin", "HEAD:tickets", cwd=tracker)
    _git("checkout", "-q", "tickets", cwd=tracker)
    _git_q("reset", "--hard", base, cwd=tracker)
    (tracker / "local-only.txt").write_text("local-side commit\n", encoding="utf-8")
    _git("add", "-A", cwd=tracker)
    _git("commit", "-qm", "local-side divergent commit", cwd=tracker)
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    assert _git_q("merge-base", "HEAD", "origin/tickets", cwd=tracker).returncode == 0, (
        "precondition: a common ancestor must exist"
    )
    assert (
        _git_q("merge-base", "--is-ancestor", "origin/tickets", "HEAD", cwd=tracker).returncode != 0
    ), "precondition: origin/tickets must NOT be an ancestor of HEAD (a real divergence)"

    line, is_issue = fsck._tracker_sync_status(str(tracker))

    assert line is not None and "DIVERGED" in line, (
        f"classifier did not report a non-fast-forward divergence; got {line!r}"
    )
    assert is_issue is True, "a non-fast-forward divergence must be a counted issue"
