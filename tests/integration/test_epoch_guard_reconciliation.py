"""Held-out real-Git oracle for the store epoch guard's two union sites."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
from rebar import _cli
from rebar._store import compat, push, sync

pytestmark = pytest.mark.integration

EPOCH_A = "2026-08-14T09-31-07Z-4f2a"
EPOCH_B = "2026-08-14T09-31-08Z-8b7c"
_AC = "Body.\n\n## Acceptance Criteria\n- [ ] x"


@dataclass(frozen=True)
class EpochRepos:
    root: Path
    tracker: Path
    origin: Path
    ticket_id: str
    base_sha: str
    local_sha: str
    remote_sha: str


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and cp.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {cp.stderr}")
    return cp


def _bare_git(origin: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--git-dir", str(origin), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _rev(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", "--verify", ref).stdout.strip()


def _origin_rev(origin: Path) -> str:
    return _bare_git(origin, "rev-parse", "refs/heads/tickets").stdout.strip()


def _record(epoch: str) -> str:
    return (
        json.dumps(
            {
                "epoch": epoch,
                "format_version": compat.CURRENT_FORMAT_VERSION,
                "required_capabilities": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _commit_file(repo: Path, relative: str, body: str, message: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", message)
    return _rev(repo)


def _build_epoch_divergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    local_epoch: str = EPOCH_A,
    remote_epoch: str = EPOCH_B,
    local_advance: bool = True,
    remote_advance: bool = True,
) -> EpochRepos:
    origin = tmp_path / "origin.git"
    root = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)],
        check=True,
        capture_output=True,
    )
    root.mkdir()
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "remote", "add", "origin", str(origin))
    monkeypatch.setenv("REBAR_ROOT", str(root))
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    rebar.init_repo(repo_root=str(root))
    ticket_id = rebar.create_ticket(
        "task",
        "epoch guard fixture",
        description=_AC,
        repo_root=str(root),
    )
    tracker = root / ".tickets-tracker"
    _commit_file(
        tracker,
        compat.COMPAT_FILENAME,
        _record(local_epoch),
        "seed local epoch",
    )
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(
        tracker,
        "fetch",
        "-q",
        "origin",
        "+refs/heads/tickets:refs/remotes/origin/tickets",
    )
    base_sha = _rev(tracker)

    remote_sha = base_sha
    if remote_advance:
        competitor = tmp_path / "competitor"
        subprocess.run(
            ["git", "clone", "-q", "-b", "tickets", str(origin), str(competitor)],
            check=True,
            capture_output=True,
        )
        _git(competitor, "config", "user.email", "remote@example.com")
        _git(competitor, "config", "user.name", "Remote")
        (competitor / compat.COMPAT_FILENAME).write_text(_record(remote_epoch), encoding="utf-8")
        remote_sha = _commit_file(
            competitor,
            "2222-remote-2222-2222/1700000000000000000-2222-remote-2222-2222-CREATE.json",
            '{"side":"remote"}\n',
            "remote epoch and event",
        )
        _git(competitor, "push", "-q", "origin", "HEAD:tickets")

    local_sha = base_sha
    if local_advance:
        local_sha = _commit_file(
            tracker,
            "1111-local-1111-1111/1700000000000000000-1111-local-1111-1111-CREATE.json",
            '{"side":"local"}\n',
            "local event",
        )
    return EpochRepos(root, tracker, origin, ticket_id, base_sha, local_sha, remote_sha)


def _assert_no_merge_state(tracker: Path) -> None:
    merge_head = Path(_git(tracker, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip())
    assert not merge_head.exists(), "epoch refusal stranded MERGE_HEAD"
    assert _git(tracker, "status", "--porcelain").stdout == ""


def test_stale_epoch_sync_refuses_union_and_preserves_both_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repos = _build_epoch_divergence(tmp_path, monkeypatch)
    local_before = _rev(repos.tracker)
    origin_before = _origin_rev(repos.origin)
    assert local_before != origin_before

    with caplog.at_level(logging.WARNING, logger="rebar._store.sync"):
        sync.reconverge(repos.tracker)

    assert _rev(repos.tracker) == local_before
    assert _origin_rev(repos.origin) == origin_before
    assert (
        _git(
            repos.tracker, "merge-base", "--is-ancestor", repos.remote_sha, "HEAD", check=False
        ).returncode
        != 0
    )
    _assert_no_merge_state(repos.tracker)
    assert "epoch" in caplog.text.lower()
    assert "re-clone" in caplog.text.lower()


def test_stale_epoch_push_refuses_merge_and_fsck_reports_diverged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repos = _build_epoch_divergence(tmp_path, monkeypatch)
    local_before = _rev(repos.tracker)
    origin_before = _origin_rev(repos.origin)

    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        push.push_tickets_branch(str(repos.tracker))

    assert _rev(repos.tracker) == local_before
    assert _origin_rev(repos.origin) == origin_before
    _assert_no_merge_state(repos.tracker)
    assert "epoch" in caplog.text.lower()
    assert "failed after 3 retries" not in caplog.text

    rc = _cli.main(["fsck"])
    fsck_output = capsys.readouterr().out
    assert rc == 1
    assert "DIVERGED" in fsck_output
    assert "PUSH_PENDING" not in fsck_output


def test_post_stash_merge_rechecks_committed_epoch_and_restores_dirty_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repos = _build_epoch_divergence(
        tmp_path,
        monkeypatch,
        local_epoch=EPOCH_A,
        remote_epoch=EPOCH_B,
    )
    record = repos.tracker / compat.COMPAT_FILENAME
    # The first guard sees a matching worktree record. The first merge must fail on this
    # dirty tracked file; after stash, the second guard sees committed EPOCH_A vs EPOCH_B.
    record.write_text(_record(EPOCH_B), encoding="utf-8")
    local_before = _rev(repos.tracker)
    origin_before = _origin_rev(repos.origin)

    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        push.push_tickets_branch(str(repos.tracker))

    assert _rev(repos.tracker) == local_before
    assert _origin_rev(repos.origin) == origin_before
    assert record.read_text(encoding="utf-8") == _record(EPOCH_B)
    assert compat.COMPAT_FILENAME in _git(repos.tracker, "status", "--porcelain").stdout
    assert _git(repos.tracker, "stash", "list").stdout == ""
    merge_head = Path(_git(repos.tracker, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip())
    assert not merge_head.exists()
    assert "epoch" in caplog.text.lower()


def test_epoch_guard_keeps_matching_epoch_merge_ff_and_ahead_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merged = _build_epoch_divergence(
        tmp_path / "merged",
        monkeypatch,
        local_epoch=EPOCH_A,
        remote_epoch=EPOCH_A,
    )
    sync.reconverge(merged.tracker)
    assert (
        _git(merged.tracker, "merge-base", "--is-ancestor", merged.local_sha, "HEAD").returncode
        == 0
    )
    assert (
        _git(merged.tracker, "merge-base", "--is-ancestor", merged.remote_sha, "HEAD").returncode
        == 0
    )

    behind = _build_epoch_divergence(
        tmp_path / "behind",
        monkeypatch,
        local_epoch=EPOCH_A,
        remote_epoch=EPOCH_A,
        local_advance=False,
    )
    sync.reconverge(behind.tracker)
    assert _rev(behind.tracker) == behind.remote_sha

    ahead = _build_epoch_divergence(
        tmp_path / "ahead",
        monkeypatch,
        local_epoch=EPOCH_A,
        remote_epoch=EPOCH_A,
        remote_advance=False,
    )
    ahead_before = _rev(ahead.tracker)
    sync.reconverge(ahead.tracker)
    assert _rev(ahead.tracker) == ahead_before

    stale = _build_epoch_divergence(tmp_path / "stale", monkeypatch)
    stale_before = _rev(stale.tracker)
    sync.reconverge(stale.tracker)
    assert _rev(stale.tracker) == stale_before


def test_cli_always_push_surfaces_epoch_refusal_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos = _build_epoch_divergence(tmp_path, monkeypatch)
    local_before = _rev(repos.tracker)
    origin_before = _origin_rev(repos.origin)
    env = subprocess_env()
    env.update(
        {
            "REBAR_ROOT": str(repos.root),
            "REBAR_SYNC_PULL": "off",
            "REBAR_SYNC_PUSH": "always",
        }
    )

    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "comment", repos.ticket_id, "local write"],
        cwd=repos.root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert _origin_rev(repos.origin) == origin_before
    assert _rev(repos.tracker) != local_before
    assert _git(repos.tracker, "merge-base", "--is-ancestor", local_before, "HEAD").returncode == 0
    assert (
        _git(
            repos.tracker, "merge-base", "--is-ancestor", origin_before, "HEAD", check=False
        ).returncode
        != 0
    )
    assert "epoch" in cp.stderr.lower()
    assert "re-clone" in cp.stderr.lower()


def _advance_origin_to_new_epoch(repos: EpochRepos, tmp_path: Path) -> str:
    competitor = tmp_path / "epoch-advance"
    subprocess.run(
        ["git", "clone", "-q", "-b", "tickets", str(repos.origin), str(competitor)],
        check=True,
        capture_output=True,
    )
    _git(competitor, "config", "user.email", "remote@example.com")
    _git(competitor, "config", "user.name", "Remote")
    (competitor / compat.COMPAT_FILENAME).write_text(_record(EPOCH_B), encoding="utf-8")
    advanced_sha = _commit_file(
        competitor,
        "3333-remote-3333-3333/1700000000000000001-3333-remote-3333-3333-CREATE.json",
        '{"side":"advanced-remote"}\n',
        "advance remote epoch",
    )
    _git(competitor, "push", "-q", "origin", "HEAD:tickets")
    return advanced_sha


@pytest.mark.parametrize("site", ["sync", "push"])
def test_epoch_guard_pins_the_checked_remote_tip_before_union(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    """A concurrent fetch cannot swap a checked matching epoch for a stale one."""
    repos = _build_epoch_divergence(
        tmp_path,
        monkeypatch,
        local_epoch=EPOCH_A,
        remote_epoch=EPOCH_A,
    )
    safe_remote_sha = repos.remote_sha
    unsafe_remote_sha = _advance_origin_to_new_epoch(repos, tmp_path)
    remote_tracking_ref = "refs/remotes/origin/tickets"
    _git(
        repos.tracker,
        "fetch",
        "-q",
        "origin",
        "+refs/heads/tickets:refs/remotes/origin/tickets",
    )
    _git(repos.tracker, "update-ref", remote_tracking_ref, safe_remote_sha)
    local_before = _rev(repos.tracker)

    original_problem = compat.store_epoch_problem
    moved = False

    def move_ref_after_epoch_check(tracker: str | os.PathLike[str], remote_ref: str) -> str | None:
        nonlocal moved
        problem = original_problem(tracker, remote_ref)
        if not moved:
            _git(repos.tracker, "update-ref", remote_tracking_ref, unsafe_remote_sha)
            moved = True
        return problem

    monkeypatch.setattr(compat, "store_epoch_problem", move_ref_after_epoch_check)

    if site == "sync":
        original_git = sync._git

        def sync_git(
            tracker: str, *args: str, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "fetch":
                return subprocess.CompletedProcess(args, 0, "", "")
            return original_git(tracker, *args, **kwargs)

        monkeypatch.setattr(sync, "_git", sync_git)
        sync.reconverge(repos.tracker)
    else:
        original_git = push._git
        push_attempts = 0

        def push_git(
            tracker: str, *args: str, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal push_attempts
            if args and args[0] == "push":
                push_attempts += 1
                if push_attempts == 1:
                    return subprocess.CompletedProcess(args, 1, "", "rejected non-fast-forward")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args and args[0] == "fetch":
                return subprocess.CompletedProcess(args, 0, "", "")
            return original_git(tracker, *args, **kwargs)

        monkeypatch.setattr(push, "_git", push_git)
        push.push_tickets_branch(str(repos.tracker))

    assert moved
    assert _rev(repos.tracker) != local_before
    assert (
        _git(repos.tracker, "merge-base", "--is-ancestor", safe_remote_sha, "HEAD").returncode == 0
    )
    assert (
        _git(
            repos.tracker,
            "merge-base",
            "--is-ancestor",
            unsafe_remote_sha,
            "HEAD",
            check=False,
        ).returncode
        != 0
    )
