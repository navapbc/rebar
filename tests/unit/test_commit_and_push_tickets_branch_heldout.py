"""Held-out recovery, identity, CLI, and compatibility oracle for commit-and-push."""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

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


def _bare_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
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
    return _bare_git(origin, "rev-parse", "tickets").stdout.strip()


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
        env=subprocess_env({"REBAR_SYNC_PUSH": "always"}),
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
    assert _bare_git(origin, "show", "tickets:recoverable.json").stdout == "retain me\n"
    assert _git(tracker, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize(
    ("phase", "reason"),
    [("status", "stage-failed"), ("add", "stage-failed"), ("commit", "commit-failed")],
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
    assert _bare_git(origin, "show", f"tickets:{event.name}", check=False).returncode != 0

    monkeypatch.setattr(push, "_git", original_git)
    push.commit_and_push_tickets_branch(tracker, message=f"retry {phase}", strict=True)
    assert _bare_git(origin, "show", f"tickets:{event.name}").stdout == "retain me\n"


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
    assert _bare_git(origin, "show", "tickets:lock-retry.json", check=False).returncode != 0

    monkeypatch.setattr(lock, "write_lock", original_lock)
    push.commit_and_push_tickets_branch(tracker, message="retry lock", strict=True)
    assert _bare_git(origin, "show", "tickets:lock-retry.json").stdout == "retain me\n"


def test_exclusion_failure_splits_strict_from_default_and_retry(
    tracker_and_origin: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exclusion is the FIRST gate: if lock artifacts cannot be excluded, nothing may stage.

    Committing without the exclusion installed is what would let a live lock file into the
    tickets branch, so this path must fail closed — typed for strict callers, warn-and-return
    for best-effort ones — while retaining the pending content for a later clean retry.
    """
    tracker, origin = tracker_and_origin
    event = tracker / "exclusion-retry.json"
    event.write_text("retain me\n", encoding="utf-8")
    head_before = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    original_git = push._git
    push_calls = 0

    def failing_git(base: str, *args: str, **kwargs: object):
        nonlocal push_calls
        if args and args[0] == "push":
            push_calls += 1
        if args[:3] == ("rev-parse", "--git-path", "info/exclude"):
            return subprocess.CompletedProcess(args, 1, "", "injected exclude resolve failure")
        return original_git(base, *args, **kwargs)

    monkeypatch.setattr(push, "_git", failing_git)
    error = _delivery_error(
        lambda: push.commit_and_push_tickets_branch(
            tracker, message="retry exclusion", strict=True
        ),
        "stage-failed",
    )
    assert "exclude" in str(error).lower()

    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        assert push.commit_and_push_tickets_branch(tracker, message="retry exclusion") is None
    assert "exclude" in caplog.text.lower()

    # Failed closed: nothing staged, nothing committed, nothing pushed, content retained.
    assert push_calls == 0
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head_before
    assert event.read_text(encoding="utf-8") == "retain me\n"
    assert _bare_git(origin, "show", f"tickets:{event.name}", check=False).returncode != 0

    monkeypatch.setattr(push, "_git", original_git)
    push.commit_and_push_tickets_branch(tracker, message="retry exclusion", strict=True)
    assert _bare_git(origin, "show", f"tickets:{event.name}").stdout == "retain me\n"


def test_legacy_tracker_commit_excludes_lock_artifacts_and_retry_is_clean(
    tracker_and_origin: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real commit on a legacy tracker must not capture the lock artifacts it is holding.

    The artifacts are NOT planted by the test: ``.ticket-write.lock`` and
    ``.ticket-write.lock.d`` ARE the write lock (lock.py WRITE_LOCK_NAME / MKDIR_LOCK_NAME),
    so planting them would just block the lock. They exist for real, on disk, precisely while
    the locked phase runs ``git add -A`` — which is the whole hazard. The fixture tracker has
    no .gitignore, so the exclusion installed by ``_ignore_lock_artifacts`` is the only thing
    between that ``add -A`` and a committed lock file.

    ``staged_with_lock_present`` keeps the oracle honest: without it the assertion would pass
    vacuously on a tracker where the artifacts never existed at staging time. Assert against
    the committed TREE, not the working tree — a lock artifact in the tree is what would be
    delivered to every other clone.
    """
    tracker, origin = tracker_and_origin
    event = tracker / "legacy-event.json"
    event.write_text("{}\n", encoding="utf-8")
    original_git = push._git
    staged_with_lock_present = False

    def probing_git(base: str, *args: str, **kwargs: object):
        nonlocal staged_with_lock_present
        if args[:2] == ("add", "-A"):
            staged_with_lock_present = (tracker / ".ticket-write.lock").exists() and (
                tracker / ".ticket-write.lock.d"
            ).exists()
        return original_git(base, *args, **kwargs)

    monkeypatch.setattr(push, "_git", probing_git)
    push.commit_and_push_tickets_branch(tracker, message="legacy commit", strict=True)
    monkeypatch.setattr(push, "_git", original_git)

    assert staged_with_lock_present, (
        "vacuous oracle: the lock artifacts were not on disk when `git add -A` ran"
    )
    tree = _git(tracker, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert "legacy-event.json" in tree, f"the real event must be committed; tree={tree}"
    assert not [name for name in tree if name.startswith(".ticket-write.lock")], (
        f"reserved lock artifacts entered the committed tree: {tree}"
    )
    assert _bare_git(origin, "show", "tickets:legacy-event.json").stdout == "{}\n"

    # Clean retry: excluded paths must not leave the tracker permanently dirty.
    assert _git(tracker, "status", "--porcelain").stdout == ""
    push.commit_and_push_tickets_branch(tracker, message="legacy retry", strict=True)
    assert _git(tracker, "status", "--porcelain").stdout == ""


def test_concurrent_first_use_cannot_tear_exclude_metadata(
    tracker_and_origin: tuple[Path, Path],
) -> None:
    """Racing first-time callers may duplicate a whole line, but may never tear one.

    ``_ignore_lock_artifacts`` runs BEFORE the write lock, so concurrent first use is
    reachable by construction. A torn pattern (``.ticket-write.lo``) would still look like a
    populated exclude file while silently no longer excluding the artifact, so the oracle is
    per-line integrity, not file equality.
    """
    tracker, _origin = tracker_and_origin
    exclude_path = Path(
        _git(
            tracker, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"
        ).stdout.strip()
    )
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_text("# pre-existing\n", encoding="utf-8")
    patterns = {".ticket-write.lock", ".ticket-write.lock.d/"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: push._ignore_lock_artifacts(str(tracker)), range(32)))
    assert all(results)

    lines = [line for line in exclude_path.read_text(encoding="utf-8").splitlines() if line]
    assert patterns <= set(lines), f"both patterns must be present: {lines}"
    assert set(lines) <= patterns | {"# pre-existing"}, (
        f"exclude metadata was torn or corrupted by concurrent first use: {lines}"
    )

    # Idempotent thereafter: once both patterns are present, no further call appends.
    settled = exclude_path.read_text(encoding="utf-8")
    assert push._ignore_lock_artifacts(str(tracker)) is True
    assert exclude_path.read_text(encoding="utf-8") == settled


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
    env = subprocess_env({"REBAR_SYNC_PUSH": "always"})
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
