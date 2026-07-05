"""Unit tests for _ref_lock.py — the bare-ref CAS lock primitive (task 524d).

Covers the C1 acceptance criteria:

  * acquire on a free ref; a second concurrent acquire fails (already exists);
  * read on present vs absent ref (defined contract) + blob schema round-trip;
  * release requires the correct old oid; a stale-oid release is idempotent success;
  * corrupt / empty / non-UTF-8 / missing-field / wrong-type blob decode fails closed;
  * subprocess timeout -> RefLockTimeoutError;
  * AC0: a blob-pointing refs/reconciler/* ref round-trips push+fetch through a remote;
  * a parametrized regression that the shared _is_cas_mismatch still classifies exit-128
    on refs/heads/tickets AND classifies the ref-lock delete-CAS on refs/reconciler/lock.

Module loading follows the by-path spec_from_file_location convention used across
the reconciler test suite.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
REF_LOCK_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_ref_lock.py"
ADVISORY_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_advisory_lock.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def ref_lock() -> ModuleType:
    if not REF_LOCK_PATH.exists():
        pytest.fail(f"_ref_lock.py not found at {REF_LOCK_PATH}")
    return _load("rebar_reconciler_ref_lock", REF_LOCK_PATH)


@pytest.fixture(scope="module")
def advisory() -> ModuleType:
    return _load("rebar_reconciler_advisory_lock", ADVISORY_PATH)


def _git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture()
def tmp_git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    _git(["commit", "--allow-empty", "-m", "init"], tmp_path)
    return tmp_path


@pytest.fixture()
def tmp_git_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A work repo with a bare 'origin' remote (for the distributed CAS path)."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], work)
    _git(["config", "user.name", "Test"], work)
    _git(["remote", "add", "origin", str(bare)], work)
    _git(["commit", "--allow-empty", "-m", "init"], work)
    return work, bare


# ---------------------------------------------------------------------------
# read contract
# ---------------------------------------------------------------------------


def test_read_absent_ref_is_free(ref_lock: ModuleType, tmp_git_repo: Path) -> None:
    assert ref_lock.read(tmp_git_repo, ref_lock.LOCK_REF) is None


def test_acquire_then_read_roundtrips_blob(ref_lock: ModuleType, tmp_git_repo: Path) -> None:
    oid = ref_lock.acquire(tmp_git_repo, ref_lock.LOCK_REF, holder="pass-1", lease_secs=120)
    state = ref_lock.read(tmp_git_repo, ref_lock.LOCK_REF)
    assert state is not None
    assert state.holder == "pass-1"
    assert state.lease_secs == 120
    assert state.fence == 0  # seeded to 0 on acquire
    assert state.heartbeat_ns > 0
    assert state.oid == oid


# ---------------------------------------------------------------------------
# acquire CAS
# ---------------------------------------------------------------------------


def test_second_acquire_fails_held(ref_lock: ModuleType, tmp_git_repo: Path) -> None:
    ref_lock.acquire(tmp_git_repo, ref_lock.LOCK_REF, holder="pass-1", lease_secs=120)
    with pytest.raises(ref_lock.RefLockHeldError):
        ref_lock.acquire(tmp_git_repo, ref_lock.LOCK_REF, holder="pass-2", lease_secs=120)


# ---------------------------------------------------------------------------
# release CAS (idempotent)
# ---------------------------------------------------------------------------


def test_release_with_correct_oid_deletes(ref_lock: ModuleType, tmp_git_repo: Path) -> None:
    oid = ref_lock.acquire(tmp_git_repo, ref_lock.LOCK_REF, holder="pass-1", lease_secs=120)
    assert ref_lock.release(tmp_git_repo, ref_lock.LOCK_REF, oid=oid) is True
    assert ref_lock.read(tmp_git_repo, ref_lock.LOCK_REF) is None


def test_release_with_stale_oid_is_idempotent_noop(
    ref_lock: ModuleType, tmp_git_repo: Path
) -> None:
    oid = ref_lock.acquire(tmp_git_repo, ref_lock.LOCK_REF, holder="pass-1", lease_secs=120)
    # Wrong oid -> the CAS sees a different value -> benign idempotent success.
    assert ref_lock.release(tmp_git_repo, ref_lock.LOCK_REF, oid="1" * 40) is False
    # The real lock is untouched.
    assert ref_lock.read(tmp_git_repo, ref_lock.LOCK_REF) is not None
    assert ref_lock.release(tmp_git_repo, ref_lock.LOCK_REF, oid=oid) is True


def test_release_absent_ref_is_idempotent_noop(ref_lock: ModuleType, tmp_git_repo: Path) -> None:
    assert ref_lock.release(tmp_git_repo, ref_lock.LOCK_REF, oid="1" * 40) is False


# ---------------------------------------------------------------------------
# fail-closed decode
# ---------------------------------------------------------------------------


def _plant_raw_blob(repo: Path, ref: str, raw: bytes) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=raw,
        capture_output=True,
        check=True,
    )
    oid = proc.stdout.decode().strip()
    _git(["update-ref", ref, oid, "0" * 40], repo)
    return oid


@pytest.mark.parametrize(
    "raw",
    [
        b"",  # empty
        b"\xff\xfe not utf8",  # non-UTF-8
        b"not json\n",  # invalid JSON
        b'{"holder": "p"}\n',  # missing fields
        b'{"holder":"p","lease_secs":120,"heartbeat_ns":1,"fence":-1}\n',  # negative fence
        b'{"holder":"p","lease_secs":120,"heartbeat_ns":1,"fence":true}\n',  # boolean fence
        b'{"holder":"p","lease_secs":0,"heartbeat_ns":1,"fence":0}\n',  # non-positive lease
        b"[1,2,3]\n",  # not an object
    ],
    ids=[
        "empty",
        "non-utf8",
        "bad-json",
        "missing",
        "neg-fence",
        "bool-fence",
        "zero-lease",
        "array",
    ],
)
def test_corrupt_blob_fails_closed(ref_lock: ModuleType, tmp_git_repo: Path, raw: bytes) -> None:
    _plant_raw_blob(tmp_git_repo, ref_lock.LOCK_REF, raw)
    with pytest.raises(ref_lock.RefLockCorruptError):
        ref_lock.read(tmp_git_repo, ref_lock.LOCK_REF)


# ---------------------------------------------------------------------------
# timeout -> RefLockTimeoutError (fail-closed)
# ---------------------------------------------------------------------------


def test_timeout_raises_reflocktimeout(
    ref_lock: ModuleType, tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "git", timeout=5.0)

    monkeypatch.setattr(ref_lock.subprocess, "run", _boom)
    with pytest.raises(ref_lock.RefLockTimeoutError):
        ref_lock.read(tmp_git_repo, ref_lock.LOCK_REF)


# ---------------------------------------------------------------------------
# AC0: distributed refspec round-trip through a remote (blob-pointing ref)
# ---------------------------------------------------------------------------


def test_ac0_blob_ref_roundtrips_through_remote(
    ref_lock: ModuleType, tmp_git_repo_with_remote: tuple[Path, Path]
) -> None:
    work, bare = tmp_git_repo_with_remote
    assert ref_lock.read(work, ref_lock.LOCK_REF, remote="origin") is None
    oid = ref_lock.acquire(work, ref_lock.LOCK_REF, holder="p1", lease_secs=120, remote="origin")

    # The bare remote holds a BLOB-pointing refs/reconciler/lock.
    obj_type = _git(["cat-file", "-t", oid], bare).stdout.strip()
    assert obj_type == "blob"
    ref_oid = _git(["rev-parse", ref_lock.LOCK_REF], bare).stdout.strip()
    assert ref_oid == oid

    # A fresh clone reads the same state via read(remote=...).
    fresh = bare.parent / "fresh"
    subprocess.run(["git", "clone", "-q", str(bare), str(fresh)], check=True, capture_output=True)
    state = ref_lock.read(fresh, ref_lock.LOCK_REF, remote="origin")
    assert state is not None and state.holder == "p1" and state.fence == 0

    # A second acquirer racing on the remote loses (create-only CAS rejected).
    with pytest.raises(ref_lock.RefLockHeldError):
        ref_lock.acquire(fresh, ref_lock.LOCK_REF, holder="p2", lease_secs=120, remote="origin")

    # Release against the observed oid tears the remote ref down.
    assert ref_lock.release(work, ref_lock.LOCK_REF, oid=oid, remote="origin") is True
    assert ref_lock.read(work, ref_lock.LOCK_REF, remote="origin") is None


# ---------------------------------------------------------------------------
# shared _is_cas_mismatch regression (backward compat + ref-lock generalization)
# ---------------------------------------------------------------------------


def test_is_cas_mismatch_tickets_branch_unchanged(advisory: ModuleType) -> None:
    """Default ref_name preserves the historical refs/heads/tickets exit-128 signal."""
    exc = subprocess.CalledProcessError(128, ["git", "update-ref", "refs/heads/tickets", "a", "b"])
    assert advisory._is_cas_mismatch(exc) is True
    # A non-update-ref exit-128 is NOT a CAS mismatch.
    other = subprocess.CalledProcessError(128, ["git", "commit", "-m", "x"])
    assert advisory._is_cas_mismatch(other) is False
    # An update-ref for a DIFFERENT ref is not a tickets-branch mismatch.
    wrong_ref = subprocess.CalledProcessError(
        128, ["git", "update-ref", "refs/reconciler/lock", "a", "b"]
    )
    assert advisory._is_cas_mismatch(wrong_ref) is False


def test_is_cas_mismatch_reconciler_ref(advisory: ModuleType) -> None:
    """The generalized discriminator classifies refs/reconciler/* CAS exits."""
    ref = "refs/reconciler/lock"
    # create/advance mismatch is exit 128
    create = subprocess.CalledProcessError(128, ["git", "update-ref", ref, "a", "0" * 40])
    assert advisory._is_cas_mismatch(create, ref) is True
    # delete mismatch is exit 1 with a "cannot lock ref" stderr
    delete = subprocess.CalledProcessError(1, ["git", "update-ref", "-d", ref, "b"])
    delete.stderr = f"error: cannot lock ref '{ref}': is at X but expected b"
    assert advisory._is_cas_mismatch(delete, ref) is True
    # a genuine exit-1 that is NOT a lock conflict is not misclassified
    genuine = subprocess.CalledProcessError(1, ["git", "update-ref", "-d", ref, "b"])
    genuine.stderr = "fatal: not a git repository"
    assert advisory._is_cas_mismatch(genuine, ref) is False
