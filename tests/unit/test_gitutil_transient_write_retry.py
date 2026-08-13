"""The shared store git seam retries the WRITE-side runner-FS transient, not just the READ
side (bug unheedful-custodial-bluebottle).

git's loose-object temp create under ``.git/objects/`` intermittently fails on CI runners
while writing a blob, tree or commit — ENOENT on Linux ("unable to create temporary file: No
such file or directory"), EINVAL on macOS ("… Invalid argument") — and the identical op
succeeds on retry. That signature used to be recognised only by ``event_append``'s private
``git add`` loop, so every OTHER caller of ``gitutil.run_git_write`` (the s3 doctor,
compaction, the claim/transition writers) hard-failed on the same blip. These tests pin the
symmetric behaviour: the shared seam retries the write-side signatures with the SAME bounded
budget the read-side faults get, real damage is still surfaced on the first attempt, and a
fault that outlives the budget still fails loudly.

Attempts are counted through a scripted ``run_git`` seam; no test here asserts on elapsed
time, and the retry backoff is stubbed out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import gitutil

# The verbatim CI stderr (Linux ENOENT variant) of the write-side object-DB fault.
_LINUX_ENOENT_STDERR = (
    "error: unable to create temporary file: No such file or directory\n"
    "error: 227c/1783673831282139152-3a825e61-STATUS.json: failed to insert into database\n"
    "error: unable to index file '227c/1783673831282139152-3a825e61-STATUS.json'\n"
    "fatal: adding files failed"
)

# The shape the s3 doctor's ``commit-tree`` hit, which is what filed this bug: the same
# temp-create fault with none of ``git add``'s follow-on marker lines.
_COMMIT_TREE_STDERR = "error: unable to create temporary file: No such file or directory"


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the retry backoff so the bounded budget is exercised without sleeping."""
    monkeypatch.setattr(gitutil, "_backoff_sleep", lambda _seconds: None)


def _script(
    monkeypatch: pytest.MonkeyPatch, results: list[subprocess.CompletedProcess]
) -> list[int]:
    """Feed *results* to successive ``run_git`` calls; the last one repeats. Returns a
    single-element list holding the attempt count."""
    calls = [0]

    def _fake_run_git(cwd, *args, **kwargs):
        calls[0] += 1
        return results[min(calls[0] - 1, len(results) - 1)]

    monkeypatch.setattr(gitutil, "run_git", _fake_run_git)
    return calls


def _failed(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git", "commit-tree"], 128, "", stderr)


def _ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git", "commit-tree"], 0, "deadbeef\n", "")


@pytest.mark.parametrize(
    "stderr",
    [
        _LINUX_ENOENT_STDERR,
        _COMMIT_TREE_STDERR,
        "error: unable to create temporary file: Invalid argument",
        "error: 227c/evt.json: failed to insert into database",
        "error: unable to index file '227c/evt.json'",
    ],
)
def test_write_side_stderr_is_classified_transient(stderr: str) -> None:
    """Both errno spellings and each sibling marker classify as the write-side transient,
    and therefore as a transient git fault the shared retry rides out. Keying on the
    errno-independent ``unable to create temporary file`` prefix is what makes the macOS
    EINVAL variant self-heal identically to the Linux ENOENT one."""
    assert gitutil._is_transient_object_write_error(stderr)
    assert gitutil._is_transient_git_fault(stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: loose object 4b825dc6 is corrupt",
        "error: inflate: data stream error (incorrect header check)",
        "fatal: not a valid object name: 'nope'",
        "error: Unable to create '/repo/.git/index.lock': File exists.",
    ],
)
def test_real_damage_is_not_classified_as_the_write_transient(stderr: str) -> None:
    """Corruption and ordinary errors are NOT write-side transients: a retry cannot change
    them, and masking them behind a backoff would hide the real fault. The index.lock case
    is a genuine transient, but it belongs to the SEPARATE lock retry — this predicate must
    not claim it."""
    assert not gitutil._is_transient_object_write_error(stderr)


def test_run_git_write_retries_a_write_side_temp_file_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    """The fault that aborted the s3 doctor's ``commit-tree`` now self-heals at the shared
    seam: it fails once, is retried, and the caller sees the success."""
    calls = _script(monkeypatch, [_failed(_COMMIT_TREE_STDERR), _ok()])

    result = gitutil.run_git_write(str(tmp_path), "commit-tree", "HEAD^{tree}")

    assert result.returncode == 0
    assert calls[0] == 2


def test_run_git_write_retries_the_macos_einval_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    """The errno phrase differs on macOS; the shared marker keys on the prefix, so the
    retry is identical."""
    calls = _script(
        monkeypatch,
        [_failed("error: unable to create temporary file: Invalid argument"), _ok()],
    )

    result = gitutil.run_git_write(str(tmp_path), "add", "--", "evt.json")

    assert result.returncode == 0
    assert calls[0] == 2


def test_run_git_write_does_not_retry_a_corrupt_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    """Real damage surfaces on the FIRST attempt — the bounded retry must never paper over
    a corrupt object."""
    calls = _script(monkeypatch, [_failed("fatal: loose object 4b825dc6 is corrupt")])

    result = gitutil.run_git_write(str(tmp_path), "commit-tree", "HEAD^{tree}")

    assert result.returncode == 128
    assert calls[0] == 1


def test_a_persistent_write_fault_fails_after_the_bounded_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    """A write-side signature on EVERY attempt spends the shared budget and then returns its
    failing result — self-healing a blip must not become an unbounded loop that hides a
    genuinely persistent fault."""
    calls = _script(monkeypatch, [_failed(_LINUX_ENOENT_STDERR)])

    result = gitutil.run_git_write(str(tmp_path), "commit-tree", "HEAD^{tree}")

    assert result.returncode == 128
    assert result.stderr == _LINUX_ENOENT_STDERR
    assert calls[0] == gitutil._TRANSIENT_FAULT_ATTEMPTS


def test_the_s3_doctor_inherits_the_write_side_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    """The doctor reaches git through the shared seam, so its ``commit-tree`` — the op this
    bug was filed against — now rides out the fault instead of aborting the heal with
    ``S3DoctorConflict``."""
    from rebar._store import s3_doctor

    calls = _script(monkeypatch, [_failed(_COMMIT_TREE_STDERR), _ok()])

    result = s3_doctor._git(str(tmp_path), "commit-tree", "HEAD^{tree}", "-p", "HEAD")

    assert result.returncode == 0
    assert result.stdout.strip() == "deadbeef"
    assert calls[0] == 2


def test_event_append_reuses_the_shared_write_marker_family() -> None:
    """Exactly ONE definition of the write-side markers: event_append's historical
    classifier IS gitutil's, not a second copy that can drift."""
    from rebar._store import event_append

    assert event_append._is_transient_add_error is gitutil._is_transient_object_write_error
