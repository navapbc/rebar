"""Held-out state-matrix and push-control oracle for the store epoch guard."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from rebar._store import compat, push

pytestmark = pytest.mark.unit

EPOCH_A = "2026-08-14T09-31-07Z-4f2a"
EPOCH_B = "2026-08-14T09-31-08Z-8b7c"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _record(epoch: object = ...) -> str:
    body: dict[str, object] = {
        "format_version": compat.CURRENT_FORMAT_VERSION,
        "required_capabilities": [],
    }
    if epoch is not ...:
        body["epoch"] = epoch
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def _tracker_with_records(
    tmp_path: Path,
    *,
    local: str | None,
    remote: str | None,
) -> Path:
    """Create a readable remote ref, then independently materialize local record bytes."""
    tracker = tmp_path / "tracker"
    subprocess.run(
        ["git", "init", "-q", "-b", "tickets", str(tracker)],
        check=True,
        capture_output=True,
    )
    _git(tracker, "config", "user.email", "t@example.com")
    _git(tracker, "config", "user.name", "T")
    record = tracker / compat.COMPAT_FILENAME
    if remote is not None:
        record.write_text(remote, encoding="utf-8")
    (tracker / "remote-ref-exists").write_text("yes\n", encoding="utf-8")
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "remote record")
    _git(tracker, "update-ref", "refs/remotes/origin/tickets", "HEAD")
    if local is None:
        record.unlink(missing_ok=True)
    else:
        record.write_text(local, encoding="utf-8")
    return tracker


@pytest.mark.parametrize(
    ("local", "remote"),
    [
        pytest.param(None, None, id="both-records-absent"),
        pytest.param(None, _record(), id="local-record-absent-remote-epoch-absent"),
        pytest.param(_record(), None, id="local-epoch-absent-remote-record-absent"),
        pytest.param(_record(), _record(), id="both-epochs-absent"),
        pytest.param(_record(EPOCH_A), _record(EPOCH_A), id="equal-epochs"),
    ],
)
def test_compatible_epoch_matrix_rows_allow_union(
    tmp_path: Path, local: str | None, remote: str | None
) -> None:
    tracker = _tracker_with_records(tmp_path, local=local, remote=remote)

    assert compat.store_epoch_problem(tracker, "origin/tickets") is None


@pytest.mark.parametrize(
    ("local", "remote", "message"),
    [
        pytest.param(_record(EPOCH_A), _record(EPOCH_B), "mismatch", id="different-epochs"),
        pytest.param(_record(), _record(EPOCH_A), "mismatch", id="stale-local"),
        pytest.param(_record(EPOCH_A), _record(), "mismatch", id="stale-remote"),
        pytest.param("{broken", _record(EPOCH_A), "local", id="corrupt-local-json"),
        pytest.param(_record(EPOCH_A), "{broken", "remote", id="corrupt-remote-json"),
        pytest.param(_record(7), _record(EPOCH_A), "string", id="non-string-local-epoch"),
        pytest.param(_record(EPOCH_A), _record(None), "string", id="non-string-remote-epoch"),
    ],
)
def test_incompatible_epoch_matrix_rows_refuse_union(
    tmp_path: Path,
    local: str,
    remote: str,
    message: str,
) -> None:
    tracker = _tracker_with_records(tmp_path, local=local, remote=remote)

    problem = compat.store_epoch_problem(tracker, "origin/tickets")

    assert problem is not None
    assert message in problem.lower()
    assert "re-clone" in problem.lower()


def test_remote_ref_unreadable_is_distinct_from_absent_record(tmp_path: Path) -> None:
    tracker = _tracker_with_records(tmp_path, local=None, remote=None)

    absent_problem = compat.store_epoch_problem(tracker, "origin/tickets")
    unreadable_problem = compat.store_epoch_problem(tracker, "origin/does-not-exist")

    assert absent_problem is None
    assert unreadable_problem is not None
    assert "remote ref" in unreadable_problem.lower()
    assert "unreadable" in unreadable_problem.lower()


def test_remote_record_is_read_from_git_ref_not_local_worktree(tmp_path: Path) -> None:
    tracker = _tracker_with_records(
        tmp_path,
        local=_record(EPOCH_A),
        remote=_record(EPOCH_B),
    )

    problem = compat.store_epoch_problem(tracker, "origin/tickets")

    assert problem is not None
    assert EPOCH_A in problem
    assert EPOCH_B in problem


def test_write_compat_record_handles_all_existing_epoch_shapes(tmp_path: Path) -> None:
    observed: list[object] = []
    for index, initial_epoch in enumerate((EPOCH_A, ..., 7, None)):
        tracker = tmp_path / str(index)
        tracker.mkdir()
        record = tracker / compat.COMPAT_FILENAME
        record.write_text(_record(initial_epoch), encoding="utf-8")

        compat.write_compat_record(tracker)
        compat.check_store_compat(tracker)  # old readers ignore a valid additive epoch key
        observed.append(json.loads(record.read_text(encoding="utf-8")).get("epoch", ...))

    assert observed == [EPOCH_A, ..., ..., ...]


def test_push_retry_stops_before_merge_and_second_push_on_epoch_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "root"
    tracker = root / ".tickets-tracker"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    tracker.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "tickets", str(tracker)],
        check=True,
        capture_output=True,
    )
    _git(tracker, "config", "user.email", "t@example.com")
    _git(tracker, "config", "user.name", "T")
    record = tracker / compat.COMPAT_FILENAME
    record.write_text(_record(EPOCH_B), encoding="utf-8")
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "remote epoch")
    _git(tracker, "update-ref", "refs/remotes/origin/tickets", "HEAD")
    record.write_text(_record(EPOCH_A), encoding="utf-8")

    calls: list[tuple[str, ...]] = []

    def fake_git(_base: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args and args[0] == "push":
            return subprocess.CompletedProcess(args, 1, "", "rejected non-fast-forward")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    monkeypatch.setattr(push, "_git", fake_git)

    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        push.push_tickets_branch(str(tracker))

    pushes = [args for args in calls if args and args[0] == "push"]
    merges = [args for args in calls if args and args[0] == "merge"]
    assert len(pushes) == 1
    assert merges == []
    assert "epoch" in caplog.text.lower()
    assert "refus" in caplog.text.lower()
    assert "failed after 3 retries" not in caplog.text
