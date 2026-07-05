"""Unit tests for the C2 lease self-healing added to _ref_lock.py (task 7522).

Covers the acceptance criteria:

  * renew() bumps heartbeat_ns + fence (owner-only CAS) and returns the new oid;
  * renew() raises LeaseLostError when the ref is absent / moved, and when the CAS
    is rejected — never silently retrying into another holder's lock;
  * a contender CAS-breaks ONLY a stale lease (no oid/fence progress across one
    lease); a live, heartbeating holder is never stolen from; a free ref is never
    "stolen" (that path is acquire);
  * double-break: two contenders race one expired lock -> exactly one winner, the
    loser observes CAS failure and does NOT acquire;
  * heartbeat_interval = max(1, lease // 3);
  * the one-lease wait is injectable (sleep_fn) and receives the HOLDER's lease;
  * a corrupt/unreadable blob during steal/renew fails closed (never stolen).
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
REF_LOCK_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_ref_lock.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def rl() -> ModuleType:
    if not REF_LOCK_PATH.exists():
        pytest.fail(f"_ref_lock.py not found at {REF_LOCK_PATH}")
    return _load("rebar_reconciler_ref_lock_lease", REF_LOCK_PATH)


def _git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    _git(["commit", "--allow-empty", "-m", "init"], tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# heartbeat cadence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lease,expected", [(120, 40), (3, 1), (1, 1), (0.5, 1), (9, 3)])
def test_heartbeat_interval(rl: ModuleType, lease: float, expected: int) -> None:
    assert rl.heartbeat_interval(lease) == expected


# ---------------------------------------------------------------------------
# renew()
# ---------------------------------------------------------------------------


def test_renew_bumps_fence_and_heartbeat(rl: ModuleType, repo: Path) -> None:
    oid = rl.acquire(repo, rl.LOCK_REF, holder="p1", lease_secs=120)
    before = rl.read(repo, rl.LOCK_REF)
    new_oid = rl.renew(repo, rl.LOCK_REF, oid=oid)
    after = rl.read(repo, rl.LOCK_REF)
    assert new_oid != oid and after.oid == new_oid
    assert after.fence == before.fence + 1 == 1
    assert after.holder == "p1" and after.lease_secs == 120
    assert after.heartbeat_ns >= before.heartbeat_ns


def test_renew_after_ref_deleted_raises_lease_lost(rl: ModuleType, repo: Path) -> None:
    oid = rl.acquire(repo, rl.LOCK_REF, holder="p1", lease_secs=120)
    rl.release(repo, rl.LOCK_REF, oid=oid)  # ref now absent
    with pytest.raises(rl.LeaseLostError):
        rl.renew(repo, rl.LOCK_REF, oid=oid)


def test_renew_with_stale_oid_raises_lease_lost(rl: ModuleType, repo: Path) -> None:
    oid = rl.acquire(repo, rl.LOCK_REF, holder="p1", lease_secs=120)
    moved = rl.renew(repo, rl.LOCK_REF, oid=oid)  # advance the ref
    assert moved != oid
    # Renewing against the OLD oid must not silently retry into the new lock.
    with pytest.raises(rl.LeaseLostError):
        rl.renew(repo, rl.LOCK_REF, oid=oid)


# ---------------------------------------------------------------------------
# steal / try_break_if_stale
# ---------------------------------------------------------------------------


def test_live_holder_is_never_stolen(rl: ModuleType, repo: Path) -> None:
    """A holder that heartbeats between the observation and the break is not stolen."""
    oid = rl.acquire(repo, rl.LOCK_REF, holder="holder", lease_secs=120)
    first = rl.read(repo, rl.LOCK_REF)
    rl.renew(repo, rl.LOCK_REF, oid=oid)  # progress: oid + fence advance
    assert rl.try_break_if_stale(repo, rl.LOCK_REF, first=first, holder="thief") is None
    assert rl.read(repo, rl.LOCK_REF).holder == "holder"


def test_stale_lease_is_broken(rl: ModuleType, repo: Path) -> None:
    rl.acquire(repo, rl.LOCK_REF, holder="holder", lease_secs=120)
    first = rl.read(repo, rl.LOCK_REF)  # no progress happens after this
    new_oid = rl.try_break_if_stale(repo, rl.LOCK_REF, first=first, holder="thief")
    assert new_oid is not None
    after = rl.read(repo, rl.LOCK_REF)
    assert after.holder == "thief" and after.fence == first.fence + 1 and after.oid == new_oid


def test_double_break_single_winner(rl: ModuleType, repo: Path) -> None:
    rl.acquire(repo, rl.LOCK_REF, holder="holder", lease_secs=120)
    first = rl.read(repo, rl.LOCK_REF)
    w1 = rl.try_break_if_stale(repo, rl.LOCK_REF, first=first, holder="t1")
    w2 = rl.try_break_if_stale(repo, rl.LOCK_REF, first=first, holder="t2")
    winners = [w for w in (w1, w2) if w is not None]
    assert len(winners) == 1, f"exactly one contender must win, got {(w1, w2)}"


def test_steal_free_ref_returns_none(rl: ModuleType, repo: Path) -> None:
    assert rl.steal(repo, rl.LOCK_REF, holder="t", sleep_fn=lambda s: None) is None


def test_steal_waits_holders_lease(rl: ModuleType, repo: Path) -> None:
    """The injected wait receives the HOLDER's lease (skew-proof relative duration)."""
    rl.acquire(repo, rl.LOCK_REF, holder="holder", lease_secs=7)
    waited: list[float] = []
    new_oid = rl.steal(repo, rl.LOCK_REF, holder="thief", sleep_fn=waited.append)
    assert waited == [7]  # waited exactly one holder-lease, on our own clock
    assert new_oid is not None and rl.read(repo, rl.LOCK_REF).holder == "thief"


def test_steal_of_live_holder_returns_none(rl: ModuleType, repo: Path) -> None:
    oid = rl.acquire(repo, rl.LOCK_REF, holder="holder", lease_secs=120)

    def _heartbeat_during_wait(_secs: float) -> None:
        rl.renew(repo, rl.LOCK_REF, oid=oid)  # holder makes progress during our wait

    assert rl.steal(repo, rl.LOCK_REF, holder="thief", sleep_fn=_heartbeat_during_wait) is None
    assert rl.read(repo, rl.LOCK_REF).holder == "holder"


# ---------------------------------------------------------------------------
# fail-closed: a corrupt blob is never stolen / renewed over
# ---------------------------------------------------------------------------


def _plant_corrupt(rl: ModuleType, repo: Path) -> None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=b"not json\n",
        capture_output=True,
        check=True,
    )
    _git(["update-ref", rl.LOCK_REF, proc.stdout.decode().strip(), "0" * 40], repo)


def test_corrupt_blob_not_stolen(rl: ModuleType, repo: Path) -> None:
    _plant_corrupt(rl, repo)
    with pytest.raises(rl.RefLockCorruptError):
        rl.steal(repo, rl.LOCK_REF, holder="thief", sleep_fn=lambda s: None)


def test_corrupt_blob_not_renewed(rl: ModuleType, repo: Path) -> None:
    _plant_corrupt(rl, repo)
    with pytest.raises(rl.RefLockCorruptError):
        rl.renew(repo, rl.LOCK_REF, oid="whatever")


# ---------------------------------------------------------------------------
# structured log emission (AC6)
# ---------------------------------------------------------------------------


def test_steal_emits_structured_log(rl: ModuleType, repo: Path, caplog) -> None:
    rl.acquire(repo, rl.LOCK_REF, holder="holder", lease_secs=120)
    first = rl.read(repo, rl.LOCK_REF)
    with caplog.at_level(logging.INFO, logger="rebar_reconciler_ref_lock_lease"):
        rl.try_break_if_stale(repo, rl.LOCK_REF, first=first, holder="thief")
    assert any("expired lease broken" in r.message for r in caplog.records)


def test_renewal_failure_emits_structured_log(rl: ModuleType, repo: Path, caplog) -> None:
    oid = rl.acquire(repo, rl.LOCK_REF, holder="holder", lease_secs=120)
    rl.renew(repo, rl.LOCK_REF, oid=oid)  # advance so the old oid is stale
    with caplog.at_level(logging.WARNING, logger="rebar_reconciler_ref_lock_lease"):
        with pytest.raises(rl.LeaseLostError):
            rl.renew(repo, rl.LOCK_REF, oid=oid)
    assert any("lease lost" in r.message for r in caplog.records)
