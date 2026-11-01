"""Specify the S3 doctor's lossless happy path.

``heal_multi_bundle`` receives its remote through ``s3remote_factory`` and collapses two
divergent bundles to one tip that reaches every commit from both heads. Its result reports
whether healing occurred, the ref and merged SHA, and deleted bundle keys.

Held-out oracles merged from test_s3_doctor_heldout: Hold out the S3 doctor's edge, crash, race,
conflict, and push-path contracts.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import stat
from pathlib import Path

import pytest
import s3_doctor_harness as h

pytestmark = pytest.mark.integration


def _install_git_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> Path:
    """Put a fake ``git`` first on ``PATH`` that fails ``bundle create`` per *mode*.

    Injects the fault at the PROCESS boundary rather than by patching a rebar internal, so
    the test pins the observable behaviour of the heal and stays valid however the module
    reaches git. Every other git invocation is exec'd straight through to the real binary.
    Modes: ``transient-once`` (only the first bundle attempt fails, the retry succeeds),
    ``transient-always``, ``non-transient``. Returns the attempt-log path, whose line count
    is the number of ``bundle create`` invocations git actually received.
    """
    real_git = shutil.which("git")
    assert real_git, "no real git on PATH"
    shim_dir = tmp_path / "gitshim"
    shim_dir.mkdir()
    attempts = tmp_path / "bundle-attempts.log"
    shim = shim_dir / "git"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"REAL = {real_git!r}\n"
        f"ATTEMPTS = {str(attempts)!r}\n"
        f"MODE = {mode!r}\n"
        "argv = sys.argv[1:]\n"
        'if "bundle" in argv and "create" in argv:\n'
        '    with open(ATTEMPTS, "a") as fh:\n'
        '        fh.write("attempt\\n")\n'
        "    with open(ATTEMPTS) as fh:\n"
        "        n = sum(1 for _ in fh)\n"
        '    if MODE == "non-transient":\n'
        '        sys.stderr.write("fatal: Refusing to create empty bundle.\\n")\n'
        "        sys.exit(128)\n"
        '    if MODE == "transient-always" or n == 1:\n'
        # Match the transient CI failure's stderr exactly.
        '        sys.stderr.write("fatal: bad object HEAD\\n")\n'
        "        sys.exit(128)\n"
        "os.execv(REAL, [REAL, *argv])\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    return attempts


def _bundle_attempts(attempts: Path) -> int:
    return len(attempts.read_text().splitlines()) if attempts.exists() else 0


def _seed_two_divergent_bundles(tmp_path: Path):
    """Build a common base then two divergent heads A, B; bundle each under ref 'tickets'.

    ``base_path`` is cloned from the content repo at the base commit, so it shares the EXACT
    common ancestor of both bundle heads. Returns (remote, base_path, sha_base, sha_a, sha_b)."""
    objdir = tmp_path / "objstore"
    remote = h.FakeS3Remote(objdir)

    content = h.init_repo(tmp_path / "content")
    sha_base = h.commit_file(content, "base.txt", "base", "base")

    # base_path shares the exact base commit (clone at base, before A/B exist).
    h.git(tmp_path, "clone", "-q", "-b", "tickets", str(content), "tracker")
    base_path = tmp_path / "tracker"
    h.git(base_path, "remote", "remove", "origin")
    h.git(base_path, "remote", "add", "origin", "s3://test-bucket/tickets")

    # Head A above base.
    h.commit_file(content, "a.txt", "A", "commit A")
    sha_a = h.seed_bundle(remote, content, "tickets", "tickets")

    # Reset to base, build head B above base (divergent from A).
    h.git(content, "reset", "-q", "--hard", sha_base)
    h.commit_file(content, "b.txt", "B", "commit B")
    sha_b = h.seed_bundle(remote, content, "tickets", "tickets")

    return remote, base_path, sha_base, sha_a, sha_b


def test_heal_collapses_two_bundles_losslessly(tmp_path: Path) -> None:
    """Two divergent bundles -> exactly one bundle, both unique commits reachable, clone works."""
    remote, base_path, _sha_base, sha_a, sha_b = _seed_two_divergent_bundles(tmp_path)

    assert len(h.bundle_keys(remote, "tickets")) == 2  # precondition: the multi-bundle state

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )

    # Postcondition 1: collapsed to exactly one bundle.
    assert len(h.bundle_keys(remote, "tickets")) == 1
    assert result["healed"] is True

    # Postcondition 2: a fresh clone of the single healed bundle succeeds, and every unique
    # commit from BOTH heads is reachable from the healed tip (no head discarded).
    clone, _tip = h.healed_tip(remote, "tickets", tmp_path)
    assert h.reachable(clone, "HEAD", sha_a), "commit A dropped by the heal"
    assert h.reachable(clone, "HEAD", sha_b), "commit B dropped by the heal"


def test_heal_rides_out_a_transient_bad_object_from_the_bundle_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry a transient ``fatal: bad object HEAD`` read and complete the lossless heal.

    The first bundle attempt reproduces the observed object-database fault. The identical,
    idempotent retry succeeds.
    """
    remote, base_path, _sha_base, sha_a, sha_b = _seed_two_divergent_bundles(tmp_path)
    attempts = _install_git_shim(tmp_path, monkeypatch, "transient-once")

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )

    assert result["healed"] is True
    assert _bundle_attempts(attempts) >= 2, "the failed bundle step was never retried"
    assert len(h.bundle_keys(remote, "tickets")) == 1
    clone, _tip = h.healed_tip(remote, "tickets", tmp_path)
    assert h.reachable(clone, "HEAD", sha_a), "commit A dropped by the retried heal"
    assert h.reachable(clone, "HEAD", sha_b), "commit B dropped by the retried heal"


def test_heal_does_not_retry_a_non_transient_bundle_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git failure OUTSIDE the transient signature still surfaces at once, unretried.

    The guard on the retry above: the marker set must stay narrow enough that a genuine
    error is neither masked nor delayed.
    """
    remote, base_path, _sha_base, _sha_a, _sha_b = _seed_two_divergent_bundles(tmp_path)
    attempts = _install_git_shim(tmp_path, monkeypatch, "non-transient")

    from rebar._store.s3_doctor import S3DoctorConflict, heal_multi_bundle

    with pytest.raises(S3DoctorConflict):
        heal_multi_bundle(
            str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
        )

    assert _bundle_attempts(attempts) == 1, "a non-transient failure must not be retried"


def test_persistent_bad_object_reports_an_object_read_failure_not_a_heads_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the transient never clears, the error names the real fault.

    ``S3DoctorConflict``'s default hint sends the operator to ``rebar store fsck-recover``, which
    is the wrong tool for a merge that never conflicted: the merge succeeded and the object
    store could not be READ. The raised error distinguishes the two.
    """
    remote, base_path, _sha_base, _sha_a, _sha_b = _seed_two_divergent_bundles(tmp_path)
    attempts = _install_git_shim(tmp_path, monkeypatch, "transient-always")

    from rebar._store.s3_doctor import S3DoctorConflict, heal_multi_bundle

    with pytest.raises(S3DoctorConflict) as excinfo:
        heal_multi_bundle(
            str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
        )

    assert _bundle_attempts(attempts) > 1, "the transient signature was never retried"
    hint = excinfo.value.hint
    assert "fsck-recover" not in hint, "an unreadable object is not a heads conflict"
    assert "object" in hint.lower()


def _bundle_contains(remote, ref: str, sha: str, scratch: Path) -> bool:
    """True if *sha* is an object in the ONE published bundle for *ref*."""
    dest = scratch / f"probe-{sha[:8]}"
    h.clone_from_single_bundle(remote, ref, dest)
    return h.git(dest, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def test_heal_folds_into_the_ref_from_a_side_branch_tracker(tmp_path: Path) -> None:
    """A tracker on a different branch now HEALS — and its side commit stays out of the ref."""
    remote, base_path, _sha_base, sha_a, sha_b = _seed_two_divergent_bundles(tmp_path)
    h.git(base_path, "checkout", "-q", "-b", "sidebranch")
    side = h.commit_file(base_path, "side.txt", "SIDE", "unrelated side commit")

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )

    assert result["healed"] is True
    assert len(h.bundle_keys(remote, "tickets")) == 1
    merged = result["merged_sha"]
    # Both divergent heads folded in; the side branch's commit did NOT.
    assert h.reachable(base_path, "refs/heads/tickets", sha_a)
    assert h.reachable(base_path, "refs/heads/tickets", sha_b)
    assert not h.reachable(base_path, "refs/heads/tickets", side)
    assert not _bundle_contains(remote, "tickets", side, tmp_path), "side commit was published"
    # The worktree is left exactly where it was: still on sidebranch, still at its own tip.
    assert h.git(base_path, "symbolic-ref", "--short", "HEAD").stdout.strip() == "sidebranch"
    assert h.git(base_path, "rev-parse", "HEAD").stdout.strip() == side
    assert h.git(base_path, "rev-parse", "refs/heads/tickets").stdout.strip() == merged


def test_heal_folds_into_the_ref_from_a_detached_head_tracker(tmp_path: Path) -> None:
    """Detached HEAD heals too: there is no branch to trust, and the fold no longer needs one."""
    remote, base_path, sha_base, sha_a, sha_b = _seed_two_divergent_bundles(tmp_path)
    h.git(base_path, "checkout", "-q", "--detach", sha_base)

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )

    assert result["healed"] is True
    assert len(h.bundle_keys(remote, "tickets")) == 1
    assert h.reachable(base_path, "refs/heads/tickets", sha_a)
    assert h.reachable(base_path, "refs/heads/tickets", sha_b)
    # HEAD stays detached at the base commit — the heal never moved the worktree.
    assert h.git(base_path, "rev-parse", "HEAD").stdout.strip() == sha_base
    assert h.git(base_path, "symbolic-ref", "--quiet", "HEAD", check=False).returncode != 0


def test_on_ref_heal_advances_the_worktree_and_leaves_it_clean(tmp_path: Path) -> None:
    """The ordinary tracker still ends with branch, index and worktree in agreement — the old
    ``git merge`` fold's end state. A ref moved without the worktree would show every merged
    file as deleted in ``git status``."""
    remote, base_path, _sha_base, sha_a, sha_b = _seed_two_divergent_bundles(tmp_path)
    assert h.git(base_path, "symbolic-ref", "--short", "HEAD").stdout.strip() == "tickets"

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )

    merged = result["merged_sha"]
    assert h.git(base_path, "rev-parse", "HEAD").stdout.strip() == merged
    assert h.git(base_path, "rev-parse", "refs/heads/tickets").stdout.strip() == merged
    # Tracked state only: the tracker also carries untracked runtime debris (the write lock).
    assert h.git(base_path, "status", "--porcelain", "-uno").stdout.strip() == ""
    for sha in (sha_a, sha_b):
        assert h.reachable(base_path, "HEAD", sha)


def test_heal_refuses_a_dirty_worktree_on_the_published_ref(tmp_path: Path) -> None:
    """The one refusal that survives: uncommitted TRACKED work on the ref's own worktree would
    be overwritten when the heal advances it, so refuse BEFORE downloading anything. Untracked
    files (the write lock, and any operator scratch) are not at risk and must not trip it —
    proven by every other test here, which run with the untracked lock file present."""
    remote, base_path, _sha_base, _sha_a, _sha_b = _seed_two_divergent_bundles(tmp_path)
    (base_path / "uncommitted.txt").write_text("operator WIP")
    h.git(base_path, "add", "uncommitted.txt")
    keys_before = sorted(h.bundle_keys(remote, "tickets"))

    downloads: list[str] = []
    real_download = remote.s3.download_file

    def _spy(Bucket: str, Key: str, Filename: str) -> None:
        downloads.append(Key)
        real_download(Bucket=Bucket, Key=Key, Filename=Filename)

    remote.s3.download_file = _spy  # type: ignore[method-assign]

    from rebar._store.s3_doctor import S3DoctorConflict, heal_multi_bundle

    with pytest.raises(S3DoctorConflict) as excinfo:
        heal_multi_bundle(
            str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
        )

    assert downloads == [], "the guard must run before any bundle is fetched"
    assert sorted(h.bundle_keys(remote, "tickets")) == keys_before
    assert (base_path / "uncommitted.txt").read_text() == "operator WIP"
    assert excinfo.value.hint


def test_single_bundle_store_still_reports_noop_off_the_published_ref(tmp_path: Path) -> None:
    """Nothing to heal stays a no-op: the guards must not turn a benign call into an error."""
    remote, base_path, _sha_base, _sha_a, _sha_b = _seed_two_divergent_bundles(tmp_path)
    for key in h.bundle_keys(remote, "tickets")[1:]:
        remote.s3.delete_object(Bucket=remote.bucket, Key=key)
    assert len(h.bundle_keys(remote, "tickets")) == 1
    h.git(base_path, "checkout", "-q", "-b", "sidebranch")

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )

    assert result["healed"] is False
    assert result["reason"] == "noop"


class _ReadOnlyS3Client:
    """A reader identity's view of the bucket: reads pass through, every write is denied."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def download_file(self, **kwargs) -> None:
        self._inner.download_file(**kwargs)

    def list_objects_v2(self, **kwargs) -> dict:
        return self._inner.list_objects_v2(**kwargs)

    def put_object(self, **_kwargs) -> None:
        raise PermissionError("AccessDenied: reader profile cannot PutObject")

    def delete_object(self, **_kwargs) -> None:
        raise PermissionError("AccessDenied: reader profile cannot DeleteObject")


class _ReaderRemote(h.FakeS3Remote):
    """The same bucket opened under a read-only profile: the per-ref lock write is denied."""

    def __init__(self, writer: h.FakeS3Remote) -> None:
        super().__init__(writer.objdir, bucket=writer.bucket, prefix=writer.prefix)
        self.s3 = _ReadOnlyS3Client(writer.s3)

    def acquire_lock(self, remote_ref: str):
        raise PermissionError("AccessDenied: reader profile cannot PutObject the lock")


_READER_URL = "s3://tickets-reader@test-bucket/tickets"


_WRITER_URL = "s3://tickets-writer@test-bucket/tickets"


def _profile_factory(writer: h.FakeS3Remote, seen: list[str]):
    """Open the bucket under the profile the URL names, as git-remote-s3 does."""

    def factory(url: str):
        seen.append(url)
        return writer if url == _WRITER_URL else _ReaderRemote(writer)

    return factory


def test_heal_writes_under_the_push_url_of_a_split_remote(tmp_path: Path) -> None:
    """Issue #145: a reader fetch URL and a writer push URL -> the heal writes as the writer.

    ``git push`` uses the push URL, so the heal, which locks, writes and deletes objects, must
    open the bucket under that identity too; under the reader it cannot even take the lock.
    """
    remote, base_path, _sha_base, sha_a, sha_b = _seed_two_divergent_bundles(tmp_path)
    h.git(base_path, "remote", "set-url", "origin", _READER_URL)
    h.git(base_path, "remote", "set-url", "--push", "origin", _WRITER_URL)
    seen: list[str] = []

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=_profile_factory(remote, seen)
    )

    assert seen == [_WRITER_URL]
    assert result["healed"] is True, result
    assert len(h.bundle_keys(remote, "tickets")) == 1
    clone, _tip = h.healed_tip(remote, "tickets", tmp_path)
    assert h.reachable(clone, "HEAD", sha_a)
    assert h.reachable(clone, "HEAD", sha_b)


def test_heal_without_a_push_url_uses_the_single_remote_url(tmp_path: Path) -> None:
    """With no push URL configured the heal keeps using the remote's one URL."""
    remote, base_path, *_ = _seed_two_divergent_bundles(tmp_path)
    h.git(base_path, "remote", "set-url", "origin", _WRITER_URL)
    seen: list[str] = []

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=_profile_factory(remote, seen)
    )

    assert seen == [_WRITER_URL]
    assert result["healed"] is True, result
    assert len(h.bundle_keys(remote, "tickets")) == 1


# ── held-out oracles (merged from test_s3_doctor_heldout) ──


def _base_tracker(tmp_path: Path):
    """A content repo with a base commit, and a tracker cloned at that exact base + s3 remote."""
    remote = h.FakeS3Remote(tmp_path / "objstore")
    content = h.init_repo(tmp_path / "content")
    sha_base = h.commit_file(content, "base.txt", "base", "base")
    h.git(tmp_path, "clone", "-q", "-b", "tickets", str(content), "tracker")
    base_path = tmp_path / "tracker"
    h.git(base_path, "remote", "remove", "origin")
    h.git(base_path, "remote", "add", "origin", "s3://test-bucket/tickets")
    return remote, content, base_path, sha_base


def _head_above(content: Path, sha_base: str, name: str, val: str, remote: h.FakeS3Remote) -> str:
    h.git(content, "reset", "-q", "--hard", sha_base)
    h.commit_file(content, name, val, f"commit {name}")
    return h.seed_bundle(remote, content, "tickets", "tickets")


def test_heal_folds_three_heads_losslessly(tmp_path: Path) -> None:
    from rebar._store.s3_doctor import heal_multi_bundle

    remote, content, base_path, sha_base = _base_tracker(tmp_path)
    sha_a = _head_above(content, sha_base, "a.txt", "A", remote)
    sha_b = _head_above(content, sha_base, "b.txt", "B", remote)
    sha_c = _head_above(content, sha_base, "c.txt", "C", remote)
    assert len(h.bundle_keys(remote, "tickets")) == 3

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )
    assert result["healed"] is True
    assert len(h.bundle_keys(remote, "tickets")) == 1
    clone, _tip = h.healed_tip(remote, "tickets", tmp_path)
    for sha in (sha_a, sha_b, sha_c):
        assert h.reachable(clone, "HEAD", sha), f"{sha} dropped"


def test_heal_unions_unrelated_history_head(tmp_path: Path) -> None:
    from rebar._store.s3_doctor import heal_multi_bundle

    remote, content, base_path, sha_base = _base_tracker(tmp_path)
    sha_a = _head_above(content, sha_base, "a.txt", "A", remote)

    # An independent root (orphan) sharing NO ancestor with base.
    orphan = h.init_repo(tmp_path / "orphan")
    sha_orphan = h.commit_file(orphan, "orphan.txt", "O", "orphan root")
    key = f"{remote.prefix}/tickets/{sha_orphan}.bundle"
    bpath = remote.objdir / "o.bundle"
    h.git(orphan, "bundle", "create", str(bpath), "refs/heads/tickets")
    remote.s3.put_object(Bucket=remote.bucket, Key=key, Body=bpath.read_bytes())
    assert len(h.bundle_keys(remote, "tickets")) == 2

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )
    assert result["healed"] is True
    assert len(h.bundle_keys(remote, "tickets")) == 1
    clone, _tip = h.healed_tip(remote, "tickets", tmp_path)
    assert h.reachable(clone, "HEAD", sha_a)
    assert h.reachable(clone, "HEAD", sha_orphan), "unrelated head discarded"


def test_heal_conflict_aborts_without_deleting(tmp_path: Path) -> None:
    from rebar._store import s3_doctor

    remote, content, base_path, sha_base = _base_tracker(tmp_path)
    # Two heads that edit the SAME non-derived file differently -> real merge conflict.
    _head_above(content, sha_base, "shared.txt", "left value", remote)
    _head_above(content, sha_base, "shared.txt", "right value", remote)
    assert len(h.bundle_keys(remote, "tickets")) == 2
    before = set(h.bundle_keys(remote, "tickets"))

    with pytest.raises(s3_doctor.S3DoctorConflict) as ei:
        s3_doctor.heal_multi_bundle(
            str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
        )
    assert "fsck-recover" in str(ei.value.hint if hasattr(ei.value, "hint") else ei.value)
    # No original bundle deleted; nothing lost.
    assert set(h.bundle_keys(remote, "tickets")) == before


def test_heal_is_crash_idempotent(tmp_path: Path) -> None:
    from rebar._store.s3_doctor import heal_multi_bundle

    remote, content, base_path, sha_base = _base_tracker(tmp_path)
    sha_a = _head_above(content, sha_base, "a.txt", "A", remote)
    sha_b = _head_above(content, sha_base, "b.txt", "B", remote)

    # Inject a crash on the FIRST delete_object (i.e. after the merged tip was put).
    real_delete = remote.s3.delete_object
    calls = {"n": 0}

    def exploding_delete(Bucket, Key):
        calls["n"] += 1
        raise RuntimeError("simulated crash mid-delete")

    remote.s3.delete_object = exploding_delete
    with pytest.raises(RuntimeError):
        heal_multi_bundle(
            str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
        )
    assert calls["n"] >= 1, "delete must be attempted only AFTER the merged tip is put"
    # The merged tip must already be present (>=1 lossless bundle), so nothing is lost.
    keys_after_crash = h.bundle_keys(remote, "tickets")
    assert len(keys_after_crash) >= 1

    # Restore delete and re-heal from a fresh tracker at base -> converges to one lossless bundle.
    remote.s3.delete_object = real_delete
    remote.release_lock("tickets", f"{remote.prefix}/tickets/LOCK#.lock")  # clear any held lock
    h.git(tmp_path, "clone", "-q", "-b", "tickets", str(content), "tracker2")
    base2 = tmp_path / "tracker2"
    h.git(base2, "remote", "remove", "origin")
    h.git(base2, "remote", "add", "origin", "s3://test-bucket/tickets")
    heal_multi_bundle(str(base2), "origin", "tickets", s3remote_factory=h.make_factory(remote))
    assert len(h.bundle_keys(remote, "tickets")) == 1
    clone, _tip = h.healed_tip(remote, "tickets", tmp_path)
    assert h.reachable(clone, "HEAD", sha_a)
    assert h.reachable(clone, "HEAD", sha_b)


@pytest.mark.parametrize("mode", ["none", "raise"])
def test_heal_backs_off_on_lock_contention(tmp_path: Path, mode: str) -> None:
    from rebar._store.s3_doctor import heal_multi_bundle

    remote, content, base_path, sha_base = _base_tracker(tmp_path)
    _head_above(content, sha_base, "a.txt", "A", remote)
    _head_above(content, sha_base, "b.txt", "B", remote)
    before = set(h.bundle_keys(remote, "tickets"))

    if mode == "none":
        remote.acquire_lock = lambda ref: None
    else:

        class _Client(Exception):
            pass

        def _raise(ref):
            raise _Client("lock held by another client")

        remote.acquire_lock = _raise

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )
    assert result["healed"] is False
    assert result.get("reason") == "locked"
    assert set(h.bundle_keys(remote, "tickets")) == before  # untouched


def test_heal_runs_merge_under_write_lock(tmp_path: Path, monkeypatch) -> None:
    from rebar._store import lock as _lock
    from rebar._store.s3_doctor import heal_multi_bundle

    remote, content, base_path, sha_base = _base_tracker(tmp_path)
    _head_above(content, sha_base, "a.txt", "A", remote)
    _head_above(content, sha_base, "b.txt", "B", remote)

    entered = {"n": 0}
    real = _lock.write_lock

    def spy_write_lock(path, **kw):
        entered["n"] += 1
        return real(path, **kw)

    monkeypatch.setattr(_lock, "write_lock", spy_write_lock)
    heal_multi_bundle(str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote))
    assert entered["n"] >= 1, "heal must take rebar's write_lock around the local merge"


def _race_worker(objdir: str, content: str, tmp: str, idx: int, barrier, results) -> None:
    import s3_doctor_harness as hh

    from rebar._store.s3_doctor import heal_multi_bundle

    remote = hh.FakeS3Remote(Path(objdir))
    dest = Path(tmp) / f"tracker-{idx}"
    hh.git(Path(tmp), "clone", "-q", "-b", "tickets", content, f"tracker-{idx}")
    hh.git(dest, "remote", "remove", "origin")
    hh.git(dest, "remote", "add", "origin", "s3://test-bucket/tickets")
    barrier.wait()
    try:
        res = heal_multi_bundle(
            str(dest), "origin", "tickets", s3remote_factory=hh.make_factory(remote)
        )
        results[idx] = bool(res.get("healed"))
    except Exception:  # noqa: BLE001 - a conflict/None-loser is a valid "did not heal" outcome
        results[idx] = False


def test_heal_multiprocess_storm_single_winner(tmp_path: Path) -> None:
    remote, content, _base_path, sha_base = _base_tracker(tmp_path)
    sha_a = _head_above(content, sha_base, "a.txt", "A", remote)
    sha_b = _head_above(content, sha_base, "b.txt", "B", remote)

    n = 4
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(n)
    results = ctx.Manager().dict()
    procs = [
        ctx.Process(
            target=_race_worker,
            args=(str(remote.objdir), str(content), str(tmp_path), i, barrier, results),
        )
        for i in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)

    assert sum(1 for v in results.values() if v) == 1, (
        f"expected exactly one winner: {dict(results)}"
    )
    assert len(h.bundle_keys(remote, "tickets")) == 1
    clone, _tip = h.healed_tip(remote, "tickets", tmp_path)
    assert h.reachable(clone, "HEAD", sha_a) and h.reachable(clone, "HEAD", sha_b)


def test_is_multi_bundle_detects_both_signatures() -> None:
    from rebar._store import push

    assert push._is_multi_bundle('error tickets "multiple bundles exists on server"')
    assert push._is_multi_bundle("! [remote rejected] multiple updates for ref not allowed")
    assert not push._is_multi_bundle("! [rejected] (non-fast-forward)")
    assert not push._is_multi_bundle("Connection timed out")


def test_push_invokes_heal_once_then_converges(tmp_path: Path, monkeypatch) -> None:
    """A multi-bundle push reject triggers the heal exactly once, after which the push converges."""
    import subprocess

    from rebar import config
    from rebar._store import push

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    monkeypatch.setattr(push, "_push_mode", lambda _root=None: "always")
    monkeypatch.setattr(config, "tickets_branch", lambda _root=None: "tickets")
    monkeypatch.setattr(config, "tickets_remote", lambda _root=None: "origin")

    state = {"pushes": 0, "heals": 0}

    def fake_git(_base, *args, **_kw):
        if args[:2] == ("remote", "get-url"):
            return subprocess.CompletedProcess(args, 0, "s3://b/tickets\n", "")
        if args and args[0] == "push":
            state["pushes"] += 1
            if state["heals"] == 0:
                return subprocess.CompletedProcess(
                    args, 1, "", 'error tickets "multiple bundles exists on server"'
                )
            return subprocess.CompletedProcess(args, 0, "", "")  # converges post-heal
        if args[:2] == ("rev-list", "--count"):
            return subprocess.CompletedProcess(args, 0, "0\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(push, "_git", fake_git)
    monkeypatch.setattr(push, "_require_s3_helper_if_s3_url", lambda url: None)

    def fake_heal(base_path, remote, ref, **kw):
        state["heals"] += 1
        return {"healed": True, "ref": ref, "merged_sha": "deadbeef", "deleted_keys": []}

    monkeypatch.setattr("rebar._store.s3_doctor.heal_multi_bundle", fake_heal)

    push.push_tickets_branch(str(tracker))  # best-effort; must not raise
    assert state["heals"] == 1, "heal must fire exactly once (healed_once guard)"


@pytest.mark.skipif(
    shutil.which("git-remote-s3") is None,
    reason="git-remote-s3 helper binary absent (optional [s3] extra not installed in this lane)",
)
def test_git_remote_s3_api_surface_contract() -> None:
    import git_remote_s3
    from git_remote_s3 import S3Remote, parse_git_url  # noqa: F401

    for attr in ("acquire_lock", "release_lock", "get_bundles_for_ref", "init_remote_head"):
        assert hasattr(S3Remote, attr), f"git_remote_s3.S3Remote lost .{attr}"
    assert hasattr(git_remote_s3, "Doctor")


def _policy_tracker(tmp_path: Path, *, driver: bool):
    """A tracker carrying the tickets-branch merge policy: the committed ``.gitattributes``
    ``.bridge_state/* merge=ours`` line (``_commands/init.py``) plus, when *driver*, the
    ``merge.ours.driver=true`` config that makes it more than decoration. Without the driver
    git silently ignores the attribute, which is exactly what the negative half asserts."""
    remote = h.FakeS3Remote(tmp_path / "objstore")
    content = h.init_repo(tmp_path / "content")
    (content / ".bridge_state").mkdir()
    (content / ".bridge_state" / "state.json").write_text("base")
    (content / ".gitattributes").write_text(".bridge_state/* merge=ours\n")
    h.git(content, "add", "-A")
    h.git(content, "commit", "-q", "-m", "base with merge policy")
    sha_base = h.git(content, "rev-parse", "HEAD").stdout.strip()

    h.git(tmp_path, "clone", "-q", "-b", "tickets", str(content), "tracker")
    base_path = tmp_path / "tracker"
    h.git(base_path, "remote", "remove", "origin")
    h.git(base_path, "remote", "add", "origin", "s3://test-bucket/tickets")
    if driver:
        h.git(base_path, "config", "merge.ours.driver", "true")

    # Two heads that write the SAME derived file differently -> collides without the policy.
    for value in ("LEFT", "RIGHT"):
        h.git(content, "reset", "-q", "--hard", sha_base)
        (content / ".bridge_state" / "state.json").write_text(value)
        h.git(content, "add", "-A")
        h.git(content, "commit", "-q", "-m", f"derived {value}")
        h.seed_bundle(remote, content, "tickets", "tickets")
    assert len(h.bundle_keys(remote, "tickets")) == 2
    return remote, base_path


def test_fold_resolves_a_derived_file_collision_via_merge_ours(tmp_path: Path) -> None:
    """Two heads rewriting the same ``.bridge_state/*`` file must NOT conflict: the tickets
    branch maps those paths to ``merge=ours``, so the fold keeps the side it merges into.
    This pins the policy the fold relies on, independent of the merge mechanism it uses."""
    remote, base_path = _policy_tracker(tmp_path, driver=True)

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )

    assert result["healed"] is True
    assert len(h.bundle_keys(remote, "tickets")) == 1
    merged = result["merged_sha"]
    blob = h.git(base_path, "show", f"{merged}:.bridge_state/state.json").stdout
    assert "<<<<<<<" not in blob, "conflict markers were committed"
    assert blob.strip() in {"LEFT", "RIGHT"}, blob
    # Nothing is discarded: both heads stay reachable from the published tip.
    for value in ("LEFT", "RIGHT"):
        sha = h.git(
            base_path, "rev-list", "--all", "--grep", f"derived {value}", "-1"
        ).stdout.strip()
        assert sha, f"no commit for {value}"
        assert h.reachable(base_path, merged, sha), f"{value} head discarded by the fold"


def test_derived_file_collision_conflicts_without_the_ours_driver(tmp_path: Path) -> None:
    """The negative half: strip the ``ours`` driver and the very same fold raises. Proves the
    positive test above is exercising the merge policy, not an accident of the inputs."""
    remote, base_path = _policy_tracker(tmp_path, driver=False)
    before = set(h.bundle_keys(remote, "tickets"))

    from rebar._store import s3_doctor

    with pytest.raises(s3_doctor.S3DoctorConflict):
        s3_doctor.heal_multi_bundle(
            str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
        )
    assert set(h.bundle_keys(remote, "tickets")) == before
