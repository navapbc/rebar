"""Object reads in the ref-lock take the remote timeout budget (bug tearful-enormous-solitaire).

The reconcile-bridge workflow checks out with ``filter: blob:none``, so the lock payload blob
is not local and ``git cat-file`` fetches it lazily from the promisor remote. Budgeting that
read as a 5 s LOCAL op made three consecutive scheduled passes fail closed on 2026-11-03.
Every ``cat-file`` in ``_ref_lock`` therefore takes ``_REMOTE_TIMEOUT_SECS``; ref-only
operations such as ``update-ref`` keep ``_LOCAL_TIMEOUT_SECS``.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from _git_upkeep import init_bare_remote
from _subprocess_env import subprocess_env


@pytest.fixture(scope="module")
def ref_lock() -> ModuleType:
    return importlib.import_module("rebar.reconciler._ref_lock")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env=subprocess_env(),
        timeout=60,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", str(work)],
        check=True,
        capture_output=True,
        env=subprocess_env(),
        timeout=60,
    )
    _git(["config", "user.email", "test@example.com"], work)
    _git(["config", "user.name", "Test"], work)
    _git(["commit", "--allow-empty", "-m", "init"], work)
    return work


@pytest.fixture
def recorded(ref_lock: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """Record ``(subcommand, timeout)`` for every git call the ref-lock makes."""
    calls: list[tuple[str, float]] = []
    real = ref_lock._git

    def spy(repo_root: Path, args: list[str], *, timeout: float, **kwargs):
        calls.append((args[0], timeout))
        return real(repo_root, args, timeout=timeout, **kwargs)

    monkeypatch.setattr(ref_lock, "_git", spy)
    return calls


def _plant_legacy_tip(repo: Path, ref: str, payload: bytes) -> str:
    """Point *ref* straight at a payload blob, the rebar <= 0.13 layout."""
    oid = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=payload,
            capture_output=True,
            check=True,
            env=subprocess_env(),
            timeout=60,
        )
        .stdout.decode()
        .strip()
    )
    _git(["update-ref", ref, oid], repo)
    return oid


def _budgets(calls: list[tuple[str, float]], subcommand: str) -> set[float]:
    return {timeout for name, timeout in calls if name == subcommand}


def test_payload_reads_on_a_commit_tip_take_the_remote_budget(
    ref_lock: ModuleType, repo: Path, recorded: list[tuple[str, float]]
) -> None:
    tip = ref_lock.acquire(repo, ref_lock.LOCK_REF, holder="h", lease_secs=60)
    ref_lock.release(repo, ref_lock.LOCK_REF, oid=tip)
    recorded.clear()

    assert ref_lock.read(repo, ref_lock.LOCK_REF) is None

    assert _budgets(recorded, "cat-file") == {ref_lock._REMOTE_TIMEOUT_SECS}


def test_payload_reads_on_a_legacy_blob_tip_take_the_remote_budget(
    ref_lock: ModuleType, repo: Path, recorded: list[tuple[str, float]]
) -> None:
    _plant_legacy_tip(repo, ref_lock.LOCK_REF, b'{"released": true}\n')
    recorded.clear()

    assert ref_lock.read(repo, ref_lock.LOCK_REF) is None

    assert ("cat-file", ref_lock._REMOTE_TIMEOUT_SECS) in recorded
    assert _budgets(recorded, "cat-file") == {ref_lock._REMOTE_TIMEOUT_SECS}


def test_set_gate_type_probe_over_a_legacy_tip_takes_the_remote_budget(
    ref_lock: ModuleType, repo: Path, recorded: list[tuple[str, float]]
) -> None:
    _plant_legacy_tip(repo, ref_lock.GATE_REF, ref_lock._encode_gate("cleared"))
    recorded.clear()

    ref_lock.set_gate(repo, "cleared")

    assert _budgets(recorded, "cat-file") == {ref_lock._REMOTE_TIMEOUT_SECS}
    assert _budgets(recorded, "update-ref") == {ref_lock._LOCAL_TIMEOUT_SECS}


def test_set_pause_over_a_legacy_gate_tip_takes_the_remote_budget(
    ref_lock: ModuleType, repo: Path, recorded: list[tuple[str, float]]
) -> None:
    _plant_legacy_tip(repo, ref_lock.GATE_REF, ref_lock._encode_gate("cleared"))
    recorded.clear()

    ref_lock.set_pause(repo, reason="maintenance", who="op", paused_at="2026-11-03T14:00:00Z")

    assert _budgets(recorded, "cat-file") == {ref_lock._REMOTE_TIMEOUT_SECS}
    assert _budgets(recorded, "update-ref") == {ref_lock._LOCAL_TIMEOUT_SECS}


def test_read_in_a_blobless_clone_fetches_the_missing_payload(
    ref_lock: ModuleType, repo: Path, tmp_path: Path
) -> None:
    bare = tmp_path / "bare.git"
    init_bare_remote(bare)
    _git(["remote", "add", "origin", str(bare)], repo)
    tip = ref_lock.acquire(repo, ref_lock.LOCK_REF, holder="h", lease_secs=60)
    ref_lock.release(repo, ref_lock.LOCK_REF, oid=tip)
    _git(
        ["push", "origin", "HEAD:refs/heads/main", f"{ref_lock.LOCK_REF}:{ref_lock.LOCK_REF}"], repo
    )
    subprocess.run(
        ["git", "--git-dir", str(bare), "config", "uploadpack.allowFilter", "true"],
        check=True,
        capture_output=True,
        env=subprocess_env(),
        timeout=60,
    )

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", f"file://{bare}", str(clone)],
        check=True,
        capture_output=True,
        env=subprocess_env(),
        timeout=60,
    )
    _git(["fetch", "origin", f"+{ref_lock.LOCK_REF}:{ref_lock.LOCK_REF}"], clone)
    payload = _git(["rev-parse", f"{ref_lock.LOCK_REF}:lock.json"], clone).stdout.strip()
    missing = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", payload],
        check=False,
        capture_output=True,
        env=subprocess_env(GIT_NO_LAZY_FETCH="1"),
        timeout=60,
    )
    assert missing.returncode != 0, "the payload blob must start out absent from the clone"

    assert ref_lock.read(clone, ref_lock.LOCK_REF) is None
