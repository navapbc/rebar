"""Held-out recovery, identity, CLI, and compatibility oracle for commit-and-push."""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from rebar._store import lock, push

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed


@pytest.fixture
def tracker_and_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    tracker = tmp_path / "tracker"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    tracker.mkdir()
    _git(tracker, "init", "-q")
    _git(tracker, "config", "user.name", "Ambient Author")
    _git(tracker, "config", "user.email", "ambient@example.com")
    _git(tracker, "remote", "add", "origin", str(origin))
    (tracker / "seed.json").write_text("{}\n", encoding="utf-8")
    _git(tracker, "add", "seed.json")
    _git(tracker, "commit", "-q", "-m", "seed")
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(
        tracker,
        "fetch",
        "-q",
        "origin",
        "+refs/heads/tickets:refs/remotes/origin/tickets",
    )
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    return tracker, origin


def _delivery_error(call, reason: str) -> push.PushDeliveryError:
    with pytest.raises(push.PushDeliveryError) as caught:
        call()
    assert caught.value.reason == reason
    return caught.value


def _remote_head(origin: Path) -> str:
    return _git(origin, "rev-parse", "tickets").stdout.strip()


def test_clean_ahead_pushes_without_empty_commit_and_rejection_keeps_commit(
    tracker_and_origin: tuple[Path, Path], tmp_path: Path
) -> None:
    tracker, origin = tracker_and_origin
    local = tracker / "already-committed.json"
    local.write_text("{}\n", encoding="utf-8")
    _git(tracker, "add", local.name)
    _git(tracker, "commit", "-q", "-m", "local ahead")
    local_head = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    assert local_head != _remote_head(origin)
    assert _git(tracker, "status", "--porcelain").stdout == ""

    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: policy declined' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _delivery_error(
        lambda: push.commit_and_push_tickets_branch(
            tracker, message="must not create an empty commit", strict=True
        ),
        "push-policy-declined",
    )
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == local_head
    assert _git(tracker, "cat-file", "-e", f"{local_head}^{{commit}}").returncode == 0

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar._store.push",
            "commit-and-push",
            "--tracker",
            str(tracker),
            "--message",
            "must not create an empty commit",
            "--strict",
        ],
        cwd=tmp_path,
        env={**os.environ, "REBAR_SYNC_PUSH": "always"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "push-policy-declined" in completed.stderr
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == local_head

    hook.unlink()
    push.commit_and_push_tickets_branch(
        tracker, message="must not create an empty commit", strict=True
    )
    assert _remote_head(origin) == local_head
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == local_head


def test_supplied_and_ambient_identity_do_not_mutate_git_config(
    tracker_and_origin: tuple[Path, Path],
) -> None:
    tracker, _origin = tracker_and_origin
    local_config_before = _git(tracker, "config", "--local", "--list").stdout
    global_config_before = _git(tracker, "config", "--global", "--list", check=False).stdout

    (tracker / "supplied.json").write_text("{}\n", encoding="utf-8")
    push.commit_and_push_tickets_branch(
        tracker,
        message="supplied identity",
        strict=True,
        author_name="Bridge Bot",
        author_email="bridge-bot@example.com",
    )
    assert _git(tracker, "log", "-1", "--format=%an <%ae>").stdout.strip() == (
        "Bridge Bot <bridge-bot@example.com>"
    )

    (tracker / "ambient.json").write_text("{}\n", encoding="utf-8")
    push.commit_and_push_tickets_branch(
        tracker,
        message="ambient identity",
        strict=True,
    )
    assert _git(tracker, "log", "-1", "--format=%an <%ae>").stdout.strip() == (
        "Ambient Author <ambient@example.com>"
    )
    assert _git(tracker, "config", "--local", "--list").stdout == local_config_before
    assert _git(tracker, "config", "--global", "--list", check=False).stdout == (
        global_config_before
    )


def test_recovery_guard_blocks_before_staging_and_clean_retry_delivers(
    tracker_and_origin: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    tracker, origin = tracker_and_origin
    event = tracker / "recoverable.json"
    event.write_text("retain me\n", encoding="utf-8")
    status_before = _git(tracker, "status", "--porcelain=v1").stdout
    remote_before = _remote_head(origin)
    marker = tracker / ".git" / "rebase-merge"
    marker.mkdir()

    _delivery_error(
        lambda: push.commit_and_push_tickets_branch(tracker, message="guarded", strict=True),
        "merge-recovery-blocked",
    )
    assert _git(tracker, "status", "--porcelain=v1").stdout == status_before
    assert _remote_head(origin) == remote_before

    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        assert push.commit_and_push_tickets_branch(tracker, message="guarded") is None
    assert "rebase" in caplog.text.lower() or "recovery" in caplog.text.lower()
    assert _git(tracker, "status", "--porcelain=v1").stdout == status_before
    assert _remote_head(origin) == remote_before

    marker.rmdir()
    push.commit_and_push_tickets_branch(tracker, message="guarded", strict=True)
    assert _git(origin, "show", "tickets:recoverable.json").stdout == "retain me\n"
    assert _git(tracker, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize(
    ("phase", "reason"),
    [("add", "stage-failed"), ("commit", "commit-failed")],
)
def test_stage_and_commit_failures_split_strict_from_default_and_retry(
    tracker_and_origin: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    phase: str,
    reason: str,
) -> None:
    tracker, origin = tracker_and_origin
    event = tracker / f"{phase}-retry.json"
    event.write_text("retain me\n", encoding="utf-8")
    original_git = push._git
    push_calls = 0

    def failing_git(base: str, *args: str, **kwargs: object):
        nonlocal push_calls
        if args and args[0] == "push":
            push_calls += 1
        if args and args[0] == phase:
            return subprocess.CompletedProcess(args, 1, "", f"injected {phase} failure")
        return original_git(base, *args, **kwargs)

    monkeypatch.setattr(push, "_git", failing_git)
    _delivery_error(
        lambda: push.commit_and_push_tickets_branch(tracker, message=f"retry {phase}", strict=True),
        reason,
    )
    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        assert push.commit_and_push_tickets_branch(tracker, message=f"retry {phase}") is None
    assert phase in caplog.text.lower()
    assert push_calls == 0
    assert event.read_text(encoding="utf-8") == "retain me\n"
    assert _git(origin, "show", f"tickets:{event.name}", check=False).returncode != 0

    monkeypatch.setattr(push, "_git", original_git)
    push.commit_and_push_tickets_branch(tracker, message=f"retry {phase}", strict=True)
    assert _git(origin, "show", f"tickets:{event.name}").stdout == "retain me\n"


def test_lock_failure_splits_strict_from_default_and_retry(
    tracker_and_origin: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracker, origin = tracker_and_origin
    event = tracker / "lock-retry.json"
    event.write_text("retain me\n", encoding="utf-8")
    original_lock = lock.write_lock

    @contextlib.contextmanager
    def unavailable_lock(*_args: object, **_kwargs: object) -> Iterator[None]:
        raise lock.LockTimeout(0, "test holder")
        yield

    monkeypatch.setattr(lock, "write_lock", unavailable_lock)
    _delivery_error(
        lambda: push.commit_and_push_tickets_branch(tracker, message="retry lock", strict=True),
        "commit-lock-timeout",
    )
    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        assert push.commit_and_push_tickets_branch(tracker, message="retry lock") is None
    assert "lock" in caplog.text.lower()
    assert event.read_text(encoding="utf-8") == "retain me\n"
    assert _git(origin, "show", "tickets:lock-retry.json", check=False).returncode != 0

    monkeypatch.setattr(lock, "write_lock", original_lock)
    push.commit_and_push_tickets_branch(tracker, message="retry lock", strict=True)
    assert _git(origin, "show", "tickets:lock-retry.json").stdout == "retain me\n"


def test_direct_push_remains_push_only_and_preserves_dirty_tree(
    tracker_and_origin: tuple[Path, Path],
) -> None:
    tracker, origin = tracker_and_origin
    dirty = tracker / "must-remain-dirty.json"
    dirty.write_text("{}\n", encoding="utf-8")
    head_before = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    remote_before = _remote_head(origin)
    status_before = _git(tracker, "status", "--porcelain=v1").stdout

    push.push_tickets_branch(tracker, strict=True)

    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _remote_head(origin) == remote_before
    assert _git(tracker, "status", "--porcelain=v1").stdout == status_before
    assert dirty.read_text(encoding="utf-8") == "{}\n"


def test_module_cli_routes_commit_flags_and_keeps_push_action(
    tracker_and_origin: tuple[Path, Path], tmp_path: Path
) -> None:
    tracker, origin = tracker_and_origin
    (tracker / "cli-commit.json").write_text("{}\n", encoding="utf-8")
    env = {**os.environ, "REBAR_SYNC_PUSH": "always"}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar._store.push",
            "commit-and-push",
            "--tracker",
            str(tracker),
            "--message",
            "cli commit subject",
            "--strict",
            "--author-name",
            "CLI Bot",
            "--author-email",
            "cli-bot@example.com",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert _git(tracker, "log", "-1", "--format=%s|%an|%ae").stdout.strip() == (
        "cli commit subject|CLI Bot|cli-bot@example.com"
    )
    assert _remote_head(origin) == _git(tracker, "rev-parse", "HEAD").stdout.strip()

    (tracker / "push-only.json").write_text("{}\n", encoding="utf-8")
    _git(tracker, "add", "push-only.json")
    _git(tracker, "commit", "-q", "-m", "existing push action")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar._store.push",
            "push",
            "--tracker",
            str(tracker),
            "--strict",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert _remote_head(origin) == _git(tracker, "rev-parse", "HEAD").stdout.strip()
