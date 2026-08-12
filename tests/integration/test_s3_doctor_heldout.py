"""HELD-OUT oracle for the S3 auto-doctor (story 0289) — edge / E2E / crash / race / conflict.

These tests are withheld from the implementation subagent. They pin the behaviours the happy
path cannot: N>=3 lossless fold, unrelated-history union, conflict-abort-without-delete,
crash-then-reheal idempotency, the write-then-delete ordering, lock-contention back-off, the
multi-process repair storm, the push-path single-invocation wiring, and the git_remote_s3 API
surface contract.

Additional contract points pinned here (implementer implements to them):

* ``heal_multi_bundle`` writes the merged tip as a NEW bundle BEFORE deleting any original and
  never deletes the merged tip (crash-safe: >=1 lossless bundle always remains).
* On an unresolved conflict in a non-derived file it raises ``s3_doctor.S3DoctorConflict``
  (``.hint`` mentions ``fsck-recover``) and deletes NOTHING remotely.
* On lock contention (``acquire_lock`` returns ``None`` or raises the helper's ClientError) it
  returns ``{"healed": False, "reason": "locked"}`` without merging or deleting.
* At entry, <=1 bundle -> ``{"healed": False}`` (no-op).
* The local fold runs under ``rebar._store.lock.write_lock(base_path)``.
* ``push._is_multi_bundle(stderr)`` detects both signatures; the push loop invokes the heal at
  most once (``healed_once`` guard) and converges.
"""

from __future__ import annotations

import multiprocessing as mp
import shutil
from pathlib import Path

import pytest
import s3_doctor_harness as h

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# N>=3 lossless fold (iterated pairwise, not octopus)
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# Unrelated-history head (no common ancestor) -> lossless union, nothing discarded
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# Conflict in a non-derived file -> abort, delete NOTHING, raise S3DoctorConflict
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# Crash after put-merged-tip, before deletes -> lossless; next run re-heals (idempotent)
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# Lock contention -> back off without merging/deleting (both None and raised-ClientError)
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# The local fold runs under rebar's write_lock
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# Multi-process repair storm -> exactly one merges; final single lossless bundle
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# push.py detection + single-invocation wiring
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# git_remote_s3 v0.3.2 private-API surface contract (credential-less)
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# The tickets-branch merge policy (.bridge_state/* merge=ours) governs the fold
# ---------------------------------------------------------------------------------------------
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
