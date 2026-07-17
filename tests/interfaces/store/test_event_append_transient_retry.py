"""A transient git object-DB failure on `git add` is retried, not surfaced as a hard
write failure (bug vocal-dip-robin / brainy-floral-globefish).

On CI runners the loose-object temp write under a tracker's `.git/objects/`
intermittently fails while hashing a blob during `git add` — observed verbatim as
``error: unable to create temporary file: No such file or directory`` (Linux/ENOENT)
or ``… Invalid argument`` (macOS/EINVAL), followed by ``failed to insert into
database`` / ``unable to index file`` / ``fatal: adding files failed``. It is a
filesystem hiccup, not a data fault: a Gerrit ``recheck`` on the identical patchset
passes. These tests inject that exact stderr on the FIRST add and assert the write
self-heals on retry — on both the single-event and batched write paths — while a
NON-transient add failure still fails immediately (no behavior change there).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._store import event_append

# The verbatim CI stderr (Linux ENOENT variant) for a transient object-DB add failure.
_TRANSIENT_ADD_STDERR = (
    "error: unable to create temporary file: No such file or directory\n"
    "error: 227c/1783673831282139152-3a825e61-STATUS.json: failed to insert into database\n"
    "error: unable to index file '227c/1783673831282139152-3a825e61-STATUS.json'\n"
    "fatal: adding files failed"
)

# The verbatim CI stderr (macOS EINVAL variant) — the exact signature this bug
# (instant-digestive-flyingfish) was filed against on macos-latest. It differs from the
# Linux variant only in the errno phrase ("Invalid argument" vs "No such file or
# directory"); the retry classifier keys on the shared, errno-independent prefix
# "unable to create temporary file", so this variant must self-heal identically.
_MACOS_EINVAL_ADD_STDERR = (
    "error: unable to create temporary file: Invalid argument\n"
    "error: 227c/1783673831282139152-3a825e61-STATUS.json: failed to insert into database\n"
    "error: unable to index file '227c/1783673831282139152-3a825e61-STATUS.json'\n"
    "fatal: adding files failed"
)


def test_macos_einval_add_stderr_is_classified_transient() -> None:
    """Pin the classification the macos-latest self-heal relies on: the EINVAL variant
    must match the retry marker."""
    assert event_append._is_transient_add_error(_MACOS_EINVAL_ADD_STDERR)


def test_macos_einval_matches_via_errno_independent_prefix() -> None:
    """The EINVAL errno LINE on its own — with none of the follow-on
    "failed to insert" / "unable to index" marker lines — must still classify transient,
    proving coverage rests on the errno-independent "unable to create temporary file"
    prefix and not on the Linux-only "No such file or directory" phrase. This goes RED if
    that shared marker is ever tightened to the full Linux errno phrase."""
    einval_only = "error: unable to create temporary file: Invalid argument"
    assert event_append._is_transient_add_error(einval_only)


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

    def fake_run(cmd, *a, **kw):  # noqa: ANN001
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
    r = subprocess.run(["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True)
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

    r = subprocess.run(["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True)
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

    def fake_run(cmd, *a, **kw):  # noqa: ANN001
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

    def fake_run(cmd, *a, **kw):  # noqa: ANN001
        if isinstance(cmd, list) and "commit" in cmd:
            state["commits"] += 1
            if state["commits"] == 1:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr=stderr)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)


def test_single_write_retries_transient_commit_odb_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git COMMIT object-DB temp-create transient must self-heal on retry, exactly like the
    git ADD path. `_git_commit` retried only index.lock + "could not parse HEAD" — not the
    object-DB write signature — so a CI-runner FS blip during commit's loose-object write
    surfaced as a hard StoreError and dropped a concurrent locked write (the enrich-prune
    concurrency flake). Injects the object-DB signature on the FIRST commit; the write must
    self-heal on the retried commit."""
    tracker = _fresh_tracker(tmp_path, "commit-odb")
    _fail_first_commit(monkeypatch, _TRANSIENT_COMMIT_STDERR)

    rc = event_append.stage_and_commit(tracker, "tk-c", _event("u-commit"))
    assert rc == 0

    r = subprocess.run(["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True)
    assert "COMMENT tk-c" in r.stdout


def test_orphan_index_lock_under_write_lock_self_heals(tmp_path: Path) -> None:
    """A stranded YOUNG ``.git/index.lock`` must not wedge writes for 300s (Mode B cascade).

    An abnormally-terminated git subprocess (SIGKILL/OOM/FS-fault under CI pressure) can leave
    an ``index.lock`` orphan. Because every store write runs under the exclusive write lock,
    that orphan is PROVABLY not held by a live peer, so it must be reclaimed IMMEDIATELY rather
    than blocked behind ``_INDEX_LOCK_STALE_S`` (300s) — otherwise every subsequent locked
    ``append_event`` raises for ~5 minutes (the catastrophic 73%-write-loss cascade)."""
    from rebar._commands.fsck import _resolve_tracker_git_dir

    tracker = _fresh_tracker(tmp_path, "orphan-lock")
    lock = Path(_resolve_tracker_git_dir(tracker)) / "index.lock"
    lock.write_text("")  # young orphan: age ~0, far under the 300s stale threshold

    rc = event_append.stage_and_commit(tracker, "tk-o", _event("u-orphan"))
    assert rc == 0, "a locked write must reclaim the orphan index.lock and succeed"
    assert not lock.exists(), "the orphan index.lock must be reclaimed, not left to wedge"

    r = subprocess.run(["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True)
    assert "COMMENT tk-o" in r.stdout


def _loose_object_path(tracker: str, relpath: str) -> Path:
    """The on-disk loose-object path for whatever blob ``relpath`` is staged as."""
    sha = subprocess.run(
        ["git", "-C", tracker, "rev-parse", f":{relpath}"], capture_output=True, text=True
    ).stdout.strip()
    op = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-path", f"objects/{sha[:2]}/{sha[2:]}"],
        capture_output=True,
        text=True,
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
    """A vanished-object index entry left by an EARLIER write must not cascade into every
    later write (bug 4c1c / Mode D (residual of ac26) — the enrich-prune ``invalid object … Error
    building trees`` loss).

    The poison belongs to a DIFFERENT path than the current write, so the per-path unstage
    never clears it; only a full index reset to HEAD does. A new write for another ticket
    must self-heal (drop the cross-path poison, commit) rather than raise, and leave no
    lingering wedge for the write after it. The heal must also be OBSERVABLE — it logs a
    warning naming the orphaned worktree path, so a silent local↔remote divergence can't hide."""
    tracker = _fresh_tracker(tmp_path, "poison-xpath")
    event_append.stage_and_commit(tracker, "tk-0", _event("u0"))  # baseline HEAD
    poison_path = _plant_poison(tracker)  # earlier write's entry whose object vanished, staged

    with caplog.at_level("WARNING"):
        rc = event_append.stage_and_commit(tracker, "tk-1", _event("u1"))
    assert rc == 0, "a write must reset the poisoned index and commit, not cascade-fail"
    rc2 = event_append.stage_and_commit(tracker, "tk-2", _event("u2"))
    assert rc2 == 0, "the poison must be gone — no lingering cascade for the next write"

    r = subprocess.run(["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True)
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

    def vanishing_add(trk, relpaths, **kw):  # noqa: ANN001,ANN003
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

    r = subprocess.run(["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True)
    assert "COMMENT tk-1" in r.stdout
