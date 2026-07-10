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
