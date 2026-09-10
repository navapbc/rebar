"""Pin retries for transient git object-database writes.

Linux ENOENT and macOS EINVAL variants share the ``unable to create temporary file``
marker. The first ``git add`` or commit must self-heal on single and batched write paths.
non-transient failures must still surface immediately.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._store import event_append, gitutil

# The verbatim CI stderr (Linux ENOENT variant) for a transient object-DB add failure.
_TRANSIENT_ADD_STDERR = (
    "error: unable to create temporary file: No such file or directory\n"
    "error: 227c/1783673831282139152-3a825e61-STATUS.json: failed to insert into database\n"
    "error: unable to index file '227c/1783673831282139152-3a825e61-STATUS.json'\n"
    "fatal: adding files failed"
)

# macOS EINVAL differs from Linux ENOENT only after the shared, errno-independent
# ``unable to create temporary file`` marker and must classify identically.
_MACOS_EINVAL_ADD_STDERR = (
    "error: unable to create temporary file: Invalid argument\n"
    "error: 227c/1783673831282139152-3a825e61-STATUS.json: failed to insert into database\n"
    "error: unable to index file '227c/1783673831282139152-3a825e61-STATUS.json'\n"
    "fatal: adding files failed"
)


def test_macos_einval_add_stderr_is_classified_transient() -> None:
    """Pin the classification the macos-latest self-heal relies on: the EINVAL variant
    must match the retry marker."""
    assert gitutil._is_transient_object_write_error(_MACOS_EINVAL_ADD_STDERR)


def test_macos_einval_matches_via_errno_independent_prefix() -> None:
    """The EINVAL errno LINE on its own — with none of the follow-on
    "failed to insert" / "unable to index" marker lines — must still classify transient,
    proving coverage rests on the errno-independent "unable to create temporary file"
    prefix and not on the Linux-only "No such file or directory" phrase. This goes RED if
    that shared marker is ever tightened to the full Linux errno phrase."""
    einval_only = "error: unable to create temporary file: Invalid argument"
    assert gitutil._is_transient_object_write_error(einval_only)


# Pre-ref-update index-write failure from ``git add``, ``git write-tree``, or commit prep.
# HEAD has not moved, so the shared transient-write retry is safe.
_TRANSIENT_INDEX_WRITE_STDERR = "fatal: unable to write new index file"

# A post-ref-update failure occurs after HEAD moves. Retrying could duplicate the event.
# Its ``new_index file`` spelling intentionally avoids the transient marker.
_POST_REF_INDEX_WRITE_STDERR = (
    "fatal: repository has been updated, but unable to write\nnew_index file."
)


def test_index_write_stderr_is_classified_transient() -> None:
    """`fatal: unable to write new index file` — the production signature reported on
    scary-fiscal-grunion — must match the transient WRITE retry marker so the store self-heals
    instead of surfacing it as a hard write failure requiring an operator retry."""
    assert gitutil._is_transient_object_write_error(_TRANSIENT_INDEX_WRITE_STDERR)


def test_post_ref_update_index_write_stderr_is_not_transient() -> None:
    """The POST-ref-update index-write failure (`repository has been updated, but unable to
    write new_index file`) must NOT be classified transient: HEAD has already moved, so a
    blind retry could duplicate the committed event. This is the safety boundary that makes
    retrying the pre-ref-update `unable to write new index file` provably idempotent."""
    assert not gitutil._is_transient_object_write_error(_POST_REF_INDEX_WRITE_STDERR)


def _fresh_tracker(tmp_path: Path, name: str) -> str:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return str(config.tracker_dir(str(repo)))


def _event(uuid: str) -> dict:
    return {
        "timestamp": 1700000000000000000,
        "uuid": uuid,
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": "x"},
    }


def _fail_first_add(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    """Make the FIRST `git add` return *stderr* with rc=1; delegate every other git
    call (and later adds) to the real subprocess.run."""
    real_run = event_append.subprocess.run
    state = {"adds": 0}

    def fake_run(cmd, *a, **kw):
        is_add = isinstance(cmd, list) and "add" in cmd
        if is_add:
            state["adds"] += 1
            if state["adds"] == 1:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)


def test_single_write_retries_transient_add_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = _fresh_tracker(tmp_path, "single")
    _fail_first_add(monkeypatch, _TRANSIENT_ADD_STDERR)

    # The first `git add` fails transiently; the write must self-heal on retry.
    rc = event_append.stage_and_commit(tracker, "tk-1", _event("u-single"))
    assert rc == 0

    # The event is durably committed (present in HEAD's tree), proving the retry
    # actually committed rather than swallowing the failure.
    r = subprocess.run(
        ["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True, check=False
    )
    assert "COMMENT tk-1" in r.stdout


def test_single_write_retries_macos_einval_add_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The macos-latest EINVAL signature this bug was filed against self-heals on retry,
    exactly like the Linux ENOENT variant."""
    tracker = _fresh_tracker(tmp_path, "macos")
    _fail_first_add(monkeypatch, _MACOS_EINVAL_ADD_STDERR)

    rc = event_append.stage_and_commit(tracker, "tk-mac", _event("u-macos"))
    assert rc == 0

    r = subprocess.run(
        ["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True, check=False
    )
    assert "COMMENT tk-mac" in r.stdout


def test_batch_write_retries_transient_add_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = _fresh_tracker(tmp_path, "batch")
    _fail_first_add(monkeypatch, _TRANSIENT_ADD_STDERR)

    n = event_append.batch_stage_and_commit(
        tracker, [("tk-a", _event("u-a")), ("tk-b", _event("u-b"))]
    )
    assert n == 2
    r = subprocess.run(
        ["git", "-C", tracker, "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.stdout.strip() == "", "index clean after the retried batch committed"


def test_nontransient_add_failure_still_fails_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NON-transient `git add` failure (e.g. a real pathspec/permission error) is NOT
    retried — it surfaces immediately, so the retry never masks genuine faults."""
    tracker = _fresh_tracker(tmp_path, "hard")
    real_run = event_append.subprocess.run
    state = {"adds": 0}

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "add" in cmd:
            state["adds"] += 1
            return subprocess.CompletedProcess(
                cmd, 128, stdout="", stderr="fatal: pathspec did not match any files"
            )
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)

    with pytest.raises(event_append.StoreError):
        event_append.stage_and_commit(tracker, "tk-1", _event("u-hard"))
    assert state["adds"] == 1, "a non-transient add failure must NOT be retried"


# A transient object-DB temp-create fault also strikes `git commit` (which writes new
# tree + commit loose objects via the same `create_tmpfile` path as `git add`). The commit
# variant carries the same errno-independent "unable to create temporary file" marker.
_TRANSIENT_COMMIT_STDERR = (
    "error: unable to create temporary file: No such file or directory\n"
    "fatal: failed to write commit object"
)


def _fail_first_commit(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    """Make the FIRST `git commit` return *stderr* with rc=128; delegate every other git
    call (and later commits) to the real subprocess.run."""
    real_run = event_append.subprocess.run
    state = {"commits": 0}

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "commit" in cmd:
            state["commits"] += 1
            if state["commits"] == 1:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr=stderr)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)


def test_single_write_retries_transient_commit_odb_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient loose-object failure during ``git commit`` retries and commits once."""
    tracker = _fresh_tracker(tmp_path, "commit-odb")
    _fail_first_commit(monkeypatch, _TRANSIENT_COMMIT_STDERR)

    rc = event_append.stage_and_commit(tracker, "tk-c", _event("u-commit"))
    assert rc == 0

    r = subprocess.run(
        ["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True, check=False
    )
    assert "COMMENT tk-c" in r.stdout


def test_single_write_retries_transient_index_write_add_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-ref index-write failure during ``git add`` must self-heal on retry."""
    tracker = _fresh_tracker(tmp_path, "index-write-add")
    _fail_first_add(monkeypatch, _TRANSIENT_INDEX_WRITE_STDERR)

    rc = event_append.stage_and_commit(tracker, "tk-iw", _event("u-index-write"))
    assert rc == 0

    r = subprocess.run(
        ["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True, check=False
    )
    assert "COMMENT tk-iw" in r.stdout


def test_single_write_retries_transient_index_write_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same `fatal: unable to write new index file` transient on `git commit`'s
    pre-ref-update index prep must self-heal on retry, and — because HEAD had not moved when
    the marker matched — commit exactly one event (no duplicate)."""
    tracker = _fresh_tracker(tmp_path, "index-write-commit")
    _fail_first_commit(monkeypatch, _TRANSIENT_INDEX_WRITE_STDERR)

    rc = event_append.stage_and_commit(tracker, "tk-iwc", _event("u-index-write-commit"))
    assert rc == 0

    r = subprocess.run(
        ["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True, check=False
    )
    lines = [ln for ln in r.stdout.splitlines() if "COMMENT tk-iwc" in ln]
    assert len(lines) == 1, "recovery must commit exactly one event, never a duplicate"


def test_orphan_index_lock_under_write_lock_self_heals(tmp_path: Path) -> None:
    """Reclaim a young orphaned ``index.lock`` while holding the exclusive write lock.

    No live peer can own it inside this boundary, so waiting for the 300-second stale
    threshold would cascade failures across subsequent writes."""
    from rebar._commands.fsck import _resolve_tracker_git_dir

    tracker = _fresh_tracker(tmp_path, "orphan-lock")
    lock = Path(_resolve_tracker_git_dir(tracker)) / "index.lock"
    lock.write_text("")  # young orphan: age ~0, far under the 300s stale threshold

    rc = event_append.stage_and_commit(tracker, "tk-o", _event("u-orphan"))
    assert rc == 0, "a locked write must reclaim the orphan index.lock and succeed"
    assert not lock.exists(), "the orphan index.lock must be reclaimed, not left to wedge"

    r = subprocess.run(
        ["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True, check=False
    )
    assert "COMMENT tk-o" in r.stdout


def _loose_object_path(tracker: str, relpath: str) -> Path:
    """The on-disk loose-object path for whatever blob ``relpath`` is staged as."""
    sha = subprocess.run(
        ["git", "-C", tracker, "rev-parse", f":{relpath}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    op = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-path", f"objects/{sha[:2]}/{sha[2:]}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return Path(op) if os.path.isabs(op) else Path(tracker) / op


def _plant_poison(tracker: str, path: str = "tk-poison/evt.json") -> str:
    """Stage ``path`` then DELETE its loose object — an index entry whose object VANISHED,
    exactly as a gc repack / partial write under pressure leaves it. Left staged, it poisons
    every subsequent commit's tree build."""
    p = Path(tracker) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"poison":1}')
    subprocess.run(["git", "-C", tracker, "add", "--", path], check=True)
    _loose_object_path(tracker, path).unlink()
    return path


def test_cross_path_poisoned_index_self_heals(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Reset an earlier cross-path vanished-object entry before committing a new write.

    Per-path unstage cannot clear another path's poison. Recovery must drop it, commit the
    current and subsequent writes, and warn with the orphaned path so divergence is visible."""
    tracker = _fresh_tracker(tmp_path, "poison-xpath")
    event_append.stage_and_commit(tracker, "tk-0", _event("u0"))  # baseline HEAD
    poison_path = _plant_poison(tracker)  # earlier write's entry whose object vanished, staged

    with caplog.at_level("WARNING"):
        rc = event_append.stage_and_commit(tracker, "tk-1", _event("u1"))
    assert rc == 0, "a write must reset the poisoned index and commit, not cascade-fail"
    rc2 = event_append.stage_and_commit(tracker, "tk-2", _event("u2"))
    assert rc2 == 0, "the poison must be gone — no lingering cascade for the next write"

    r = subprocess.run(
        ["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True, check=False
    )
    assert "COMMENT tk-1" in r.stdout and "COMMENT tk-2" in r.stdout
    # The anomaly is recorded (not silently papered over) and names the dropped orphan.
    heal_logs = [r for r in caplog.records if "poisoned index" in r.getMessage()]
    assert heal_logs, "a poisoned-index heal must emit a warning"
    assert poison_path in heal_logs[-1].getMessage(), "the orphaned worktree path must be named"


def test_own_vanished_object_is_regenerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When THIS write's own object vanishes between add and commit (the serialized case: the
    vanishing writer is first to hit its own poison), the self-heal must REGENERATE it from the
    intact worktree file and commit — no write lost (bug 4c1c / Mode D)."""
    tracker = _fresh_tracker(tmp_path, "poison-own")
    event_append.stage_and_commit(tracker, "tk-0", _event("u0"))  # baseline HEAD

    real_add = event_append._git_add
    state = {"n": 0}

    def vanishing_add(trk, relpaths, **kw):
        res = real_add(trk, relpaths, **kw)
        state["n"] += 1
        if state["n"] == 1:  # vanish ONLY on the first add; the recovery re-add must succeed
            for rp in relpaths:
                op = _loose_object_path(trk, rp)
                if op.exists():
                    op.unlink()
        return res

    monkeypatch.setattr(event_append, "_git_add", vanishing_add)
    rc = event_append.stage_and_commit(tracker, "tk-1", _event("u1"))
    assert rc == 0, "the vanished own-object must be regenerated on re-add and commit"

    r = subprocess.run(
        ["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True, check=False
    )
    assert "COMMENT tk-1" in r.stdout
