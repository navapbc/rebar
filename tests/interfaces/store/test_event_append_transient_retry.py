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
