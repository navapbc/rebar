"""Keep remote-authoritative reconciler locks reclaimable.

Fetching a remote lock can leave a local ref after remote release. Read and
release must clear that orphan, or a free remote still appears held and steal's
lease push cannot recover it. Exercise git CAS in a bare remote and clone."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "reconciler"


@pytest.fixture(scope="module")
def ref_lock():
    return importlib.import_module("rebar.reconciler._ref_lock")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A working clone whose ``origin`` is a real bare repo (the authoritative remote)."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"], cwd=work, check=True)
    return work


def _local_ref(work: Path, ref: str) -> str | None:
    return _git(work, "rev-parse", "--verify", "--quiet", ref) or None


def _remote_ref(work: Path, ref: str) -> str | None:
    out = _git(work, "ls-remote", "origin", ref)
    return out.split()[0] if out else None


# ---------------------------------------------------------------------------
# The proven mechanism, end to end
# ---------------------------------------------------------------------------


def test_local_read_of_remote_lock_does_not_strand_a_local_ref(ref_lock, clone: Path):
    """The reported bug, reproduced exactly.

    A remote holder (the GitHub Actions bridge) holds the lock; a LOCAL pass reads it;
    the remote holder then releases. The authoritative remote is now free, so a later
    local read MUST see the lock as free.
    """
    ref = ref_lock.LOCK_REF
    oid = ref_lock.acquire(clone, ref, holder="GHA-bridge", lease_secs=120, remote="origin")
    assert _remote_ref(clone, ref) is not None, "fixture precondition: remote holds the lock"

    state = ref_lock.read(clone, ref, remote="origin")
    assert state is not None and state.holder == "GHA-bridge", (
        "fixture precondition: the local pass must observe the remote holder"
    )

    ref_lock.release(clone, ref, oid=oid, remote="origin")
    assert _remote_ref(clone, ref) is None, (
        "fixture precondition: the authoritative remote lock is released"
    )

    assert ref_lock.read(clone, ref, remote="origin") is None, (
        "remote is free but read() still reports HELD — a stranded local ref is "
        "overriding the authoritative remote (ADR 0031: 'the remote is the truth')"
    )


def test_release_in_remote_mode_leaves_no_local_ref(ref_lock, clone: Path):
    """``release`` must clear BOTH halves the ADR names, not just the remote."""
    ref = ref_lock.LOCK_REF
    oid = ref_lock.acquire(clone, ref, holder="H", lease_secs=120, remote="origin")
    ref_lock.read(clone, ref, remote="origin")  # plants the local copy
    ref_lock.release(clone, ref, oid=oid, remote="origin")

    assert _remote_ref(clone, ref) is None, "remote half released"
    assert _local_ref(clone, ref) is None, (
        "release() deleted the remote ref but left the local one behind"
    )


def test_orphan_local_ref_is_reclaimable(ref_lock, clone: Path):
    """A stranded local ref must not wedge the checkout permanently.

    ADR 0031 §"Lease self-healing": a crashed holder's lock becomes "steal-able after
    one lease interval automatically", so break-glass is "rarely necessary". Whether
    reclaimed by ``read`` treating an absent remote ref as free, or by ``steal``
    breaking it, a local pass MUST be able to proceed without manual intervention.
    """
    ref = ref_lock.LOCK_REF
    oid = ref_lock.acquire(clone, ref, holder="GHA-bridge", lease_secs=120, remote="origin")
    ref_lock.read(clone, ref, remote="origin")
    ref_lock.release(clone, ref, oid=oid, remote="origin")
    assert _remote_ref(clone, ref) is None, "fixture precondition: remote free"

    # Either the lock now reads free, or a steal reclaims it. Both are acceptable
    # outcomes; being wedged with neither is not.
    reads_free = ref_lock.read(clone, ref, remote="origin") is None
    stolen = (
        None
        if reads_free
        else ref_lock.steal(
            clone, ref, holder="local-pass", remote="origin", sleep_fn=lambda _s: None
        )
    )
    assert reads_free or stolen is not None, (
        "orphan local ref is unbreakable: read() says HELD and steal() returned None, "
        "so only a manual `git update-ref -d` can recover — contradicting ADR 0031"
    )


def test_preexisting_orphan_is_pruned_on_read(ref_lock, clone: Path):
    """The PRODUCTION shape: the orphan outlives any local release.

    The GitHub Actions bridge acquires and releases on ITS OWN runner, so this
    checkout's stranded local ref is never cleaned by a local ``release`` call — a
    plain ``read`` must reclaim it. Without this case the suite can be satisfied by
    fixing ``release`` alone, which would leave every real checkout wedged.
    """
    ref = ref_lock.LOCK_REF
    # A local ref left behind by an earlier read, with the remote authoritative-free.
    # Build it exactly the way the real fetch does, then drop the remote side without
    # going through the local release path at all.
    oid = ref_lock.acquire(clone, ref, holder="GHA-bridge", lease_secs=120, remote="origin")
    ref_lock.read(clone, ref, remote="origin")  # plants the local copy
    subprocess.run(
        ["git", "push", "-q", "origin", f":{ref}"], cwd=clone, check=True
    )  # remote-only release, as another machine would do
    assert _remote_ref(clone, ref) is None, "fixture precondition: remote is free"
    assert _local_ref(clone, ref) == oid, "fixture precondition: local orphan is stranded"

    assert ref_lock.read(clone, ref, remote="origin") is None, (
        "a pre-existing local orphan was not pruned on read: the remote is free but "
        "the lock still reads HELD, so this checkout stays wedged"
    )


def test_a_genuinely_held_remote_lock_is_still_respected(ref_lock, clone: Path):
    """Guard the opposite failure: do not make every lock look free.

    A LIVE remote holder must still block a local pass, or the fix would disable
    cross-machine mutual exclusion instead of repairing the orphan case.
    """
    ref = ref_lock.LOCK_REF
    ref_lock.acquire(clone, ref, holder="GHA-bridge", lease_secs=120, remote="origin")

    state = ref_lock.read(clone, ref, remote="origin")
    assert state is not None, "a live remote holder must read as HELD"
    assert state.holder == "GHA-bridge"
