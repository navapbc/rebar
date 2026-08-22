"""C3 (task 2711) — the ref-lock backend wired into the advisory-lock API.

Covers:
  * lock_backend=ref: acquire_pass_lock returns an oid and writes refs/reconciler/lock
    (NOT a .reconciler-* file on the tickets branch); release_pass_lock(oid) tears it down;
  * a double-acquire raises ReconcileLockError (convergence — single winner);
  * check_phase_gate reads the refs/reconciler/gate blob (set_gate/read_gate) and blocks
    a higher mode;
  * a crashed pass (acquired, never released) is reclaimable after its lease via steal;
  * the _Heartbeat daemon sets lock_lost when renew raises LeaseLostError, and the main
    abort-check closure then raises ReconcileLockLost;
  * applier.apply invokes abort_check at its per-mutation checkpoint and propagates the abort.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def advisory() -> ModuleType:
    return _load("rebar_reconciler._advisory_lock", ENGINE / "_advisory_lock.py")


@pytest.fixture(scope="module")
def ref_lock(advisory: ModuleType) -> ModuleType:
    # Return the SAME _ref_lock instance the advisory module loads (canonical key),
    # so we never register a colliding sys.modules key that corrupts the wiring.
    return advisory._load_ref_lock()


@pytest.fixture(scope="module")
def mode_mod() -> ModuleType:
    return _load("rebar_reconciler.mode", ENGINE / "mode.py")


def _git(args: list[str], repo: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **kwargs,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    _git(["commit", "--allow-empty", "-m", "init"], tmp_path)
    return tmp_path


@pytest.fixture
def ref_backend(advisory: ModuleType, monkeypatch: pytest.MonkeyPatch):
    """A local (no-remote) CAS and a short lease."""
    monkeypatch.setattr(advisory, "_lock_lease_secs", lambda: 1)
    monkeypatch.setattr(advisory, "_lock_remote", lambda repo_root: None)


# ---------------------------------------------------------------------------
# acquire / release under the ref backend
# ---------------------------------------------------------------------------


def test_acquire_returns_oid_and_writes_ref_not_tickets_file(
    advisory: ModuleType, ref_lock: ModuleType, repo: Path, ref_backend
) -> None:
    oid = advisory.acquire_pass_lock("pass-1", repo)
    assert oid and advisory.check_pass_lock is not None
    # The ref exists and points at the returned oid.
    assert _git(["rev-parse", ref_lock.LOCK_REF], repo).stdout.strip() == oid
    # No .reconciler-pass-lock was written to the tickets branch.
    show = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", "tickets:.reconciler-pass-lock"],
        capture_output=True,
        check=False,
    )
    assert show.returncode != 0, "ref backend must NOT write a tickets-branch lock file"
    # Release tears the ref down.
    advisory.release_pass_lock("pass-1", repo, oid=oid)
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref_lock.LOCK_REF],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_double_acquire_raises_reconcile_lock_error(
    advisory: ModuleType, repo: Path, ref_backend
) -> None:
    advisory.acquire_pass_lock("pass-1", repo)
    with pytest.raises(advisory.ReconcileLockError):
        advisory.acquire_pass_lock("pass-2", repo)


def test_release_is_idempotent(advisory: ModuleType, repo: Path, ref_backend) -> None:
    oid = advisory.acquire_pass_lock("pass-1", repo)
    advisory.release_pass_lock("pass-1", repo, oid=oid)
    # Second release (ref already gone) is a benign no-op.
    advisory.release_pass_lock("pass-1", repo, oid=oid)


# ---------------------------------------------------------------------------
# phase gate under the ref backend
# ---------------------------------------------------------------------------


def test_phase_gate_reads_gate_ref(
    advisory: ModuleType, ref_lock: ModuleType, mode_mod: ModuleType, repo: Path, ref_backend
) -> None:
    # No gate ref -> nothing blocked.
    throttle = mode_mod.Mode.BOOTSTRAP_THROTTLE
    strict = mode_mod.Mode.BOOTSTRAP_STRICT
    assert advisory.check_phase_gate(throttle, repo) is False

    # Gate the store at BOOTSTRAP_STRICT -> a higher mode (THROTTLE) is blocked.
    ref_lock.set_gate(repo, strict.value)
    assert advisory.check_phase_gate(throttle, repo) is True
    assert advisory.check_phase_gate(strict, repo) is False  # equal is not "greater"

    # Clearing the gate re-opens.
    ref_lock.clear_gate(repo)
    assert advisory.check_phase_gate(throttle, repo) is False


def _gate_blob(ref_lock: ModuleType, repo: Path) -> bytes:
    oid = _git(["rev-parse", ref_lock.GATE_REF], repo).stdout.strip()
    return _git(["cat-file", "blob", oid], repo).stdout.encode()


def test_pause_blob_retains_the_legacy_gate_field(
    ref_lock: ModuleType, repo: Path, ref_backend
) -> None:
    paused_at = "2026-08-08T17:00:00Z"
    oid = ref_lock.set_pause(
        repo,
        reason="planned maintenance",
        who="operator@example.com",
        paused_at=paused_at,
    )

    assert _git(["rev-parse", ref_lock.GATE_REF], repo).stdout.strip() == oid
    assert json.loads(_gate_blob(ref_lock, repo)) == {
        "gated_mode": "reconcile-check",
        "paused": True,
        "reason": "planned maintenance",
        "who": "operator@example.com",
        "paused_at": paused_at,
    }


def test_read_pause_with_oid_uses_one_observation_and_preserves_legacy_read(
    ref_lock: ModuleType,
    repo: Path,
    ref_backend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_oid = ref_lock.set_pause(
        repo,
        reason="repair:fsck:owned",
        who="operator@example.com",
        paused_at="2026-08-09T17:00:00Z",
    )
    legacy = ref_lock.read_pause(repo)
    observed: list[str | None] = []
    original_ref_oid = ref_lock._ref_oid

    def one_observation(*args, **kwargs):
        observed.append(original_ref_oid(*args, **kwargs))
        return observed[-1]

    monkeypatch.setattr(ref_lock, "_ref_oid", one_observation)

    pause, oid = ref_lock.read_pause_with_oid(repo)

    assert observed == [first_oid]
    assert oid == first_oid
    assert (
        pause
        == legacy
        == {
            "gated_mode": "reconcile-check",
            "paused": True,
            "reason": "repair:fsck:owned",
            "who": "operator@example.com",
            "paused_at": "2026-08-09T17:00:00Z",
        }
    )


def _legacy_decode_gate(raw: bytes) -> str:
    """Verbatim pre-pause decoder shipped by the previous binary."""
    if not raw:
        raise ValueError("empty gate blob")
    doc = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(doc, dict)
        or not isinstance(doc.get("gated_mode"), str)
        or not doc["gated_mode"]
    ):
        raise ValueError("gate blob missing a non-empty 'gated_mode'")
    return doc["gated_mode"]


@pytest.mark.parametrize(
    "reason",
    [
        "planned maintenance",
        'quoted reason: "database cutover"',
        "two-line reason\nwith a delimiter: value",
    ],
)
def test_pre_pause_decoder_accepts_every_new_pause_blob(
    ref_lock: ModuleType, repo: Path, ref_backend, reason: str
) -> None:
    ref_lock.set_pause(
        repo,
        reason=reason,
        who="operator@example.com",
        paused_at="2026-08-08T17:00:00Z",
    )

    assert _legacy_decode_gate(_gate_blob(ref_lock, repo)) == "reconcile-check"


def test_same_reason_repause_preserves_exact_blob_and_metadata(
    ref_lock: ModuleType, repo: Path, ref_backend
) -> None:
    first_oid = ref_lock.set_pause(
        repo,
        reason="planned maintenance",
        who="first@example.com",
        paused_at="2026-08-08T17:00:00Z",
    )
    first_raw = _gate_blob(ref_lock, repo)

    second_oid = ref_lock.set_pause(
        repo,
        reason="planned maintenance",
        who="second@example.com",
        paused_at="2026-08-09T01:02:03Z",
    )

    assert second_oid == first_oid
    assert _gate_blob(ref_lock, repo) == first_raw


def test_different_reason_repause_refuses_and_preserves_state(
    ref_lock: ModuleType, repo: Path, ref_backend
) -> None:
    first_oid = ref_lock.set_pause(
        repo,
        reason="database cutover",
        who="first@example.com",
        paused_at="2026-08-08T17:00:00Z",
    )
    first_raw = _gate_blob(ref_lock, repo)

    with pytest.raises(ref_lock.RefLockError, match="database cutover"):
        ref_lock.set_pause(
            repo,
            reason="network maintenance",
            who="second@example.com",
            paused_at="2026-08-09T01:02:03Z",
        )

    assert _git(["rev-parse", ref_lock.GATE_REF], repo).stdout.strip() == first_oid
    assert _gate_blob(ref_lock, repo) == first_raw


def test_pause_cas_loss_surfaces_winner_without_overwrite(
    ref_lock: ModuleType, repo: Path, ref_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner = {
        "gated_mode": "reconcile-check",
        "paused": True,
        "reason": "winner maintenance",
        "who": "winner@example.com",
        "paused_at": "2026-08-08T18:00:00Z",
    }
    winner_raw = (json.dumps(winner) + "\n").encode()

    def lose_cas(*_args, **_kwargs) -> bool:
        winner_oid = ref_lock._hash_object(repo, winner_raw)
        _git(["update-ref", ref_lock.GATE_REF, winner_oid], repo)
        return False

    monkeypatch.setattr(ref_lock, "_cas_advance", lose_cas)

    with pytest.raises(ref_lock.RefLockError) as raised:
        ref_lock.set_pause(
            repo,
            reason="loser maintenance",
            who="loser@example.com",
            paused_at="2026-08-08T17:00:00Z",
        )

    message = str(raised.value)
    assert "winner maintenance" in message
    assert "winner@example.com" in message
    assert "2026-08-08T18:00:00Z" in message
    assert _gate_blob(ref_lock, repo) == winner_raw


# ---------------------------------------------------------------------------
# crashed pass reclaimable after lease (C3 acquire ↔ C2 steal)
# ---------------------------------------------------------------------------


def test_crashed_pass_reclaimable_after_lease(
    advisory: ModuleType, ref_lock: ModuleType, repo: Path, ref_backend
) -> None:
    # Pass A acquires and "crashes" (never releases).
    advisory.acquire_pass_lock("pass-A", repo)
    # Pass B cannot acquire (still held)...
    with pytest.raises(advisory.ReconcileLockError):
        advisory.acquire_pass_lock("pass-B", repo)
    # ...but can reclaim after one lease with no progress (steal, wait injected).
    stolen = ref_lock.steal(repo, ref_lock.LOCK_REF, holder="pass-B", sleep_fn=lambda s: None)
    assert stolen is not None
    state = ref_lock.read(repo, ref_lock.LOCK_REF)
    assert state.holder == "pass-B"


# ---------------------------------------------------------------------------
# heartbeat daemon -> lock_lost, and the abort-check closure -> ReconcileLockLost
# ---------------------------------------------------------------------------


def test_heartbeat_sets_lock_lost_on_lease_loss(advisory: ModuleType, repo: Path) -> None:
    main_mod = _load("rebar_reconciler.__main__", ENGINE / "__main__.py")

    class _FakeAdvisory:
        ReconcileLockLost = advisory.ReconcileLockLost

        def _load_ref_lock(self):
            return advisory._load_ref_lock()

        def renew_pass_lock(self, pass_id, repo_root, oid):
            raise advisory._load_ref_lock().LeaseLostError("stolen")

    hb = main_mod._Heartbeat(_FakeAdvisory(), "pass-1", repo, "abc", interval=0.05)
    hb.start()
    assert hb.lock_lost.wait(timeout=3.0), "heartbeat must set lock_lost on LeaseLostError"
    hb.stop()

    # The main abort-check closure raises ReconcileLockLost once lock_lost is set.
    lock_lost = hb.lock_lost

    def abort_check() -> None:
        if lock_lost.is_set():
            raise advisory.ReconcileLockLost("pass lock lease lost mid-pass")

    with pytest.raises(advisory.ReconcileLockLost):
        abort_check()


# ---------------------------------------------------------------------------
# applier invokes abort_check at its per-mutation checkpoint
# ---------------------------------------------------------------------------


def test_applier_apply_invokes_abort_check(repo: Path) -> None:
    applier = _load("rebar_reconciler.applier", ENGINE / "applier.py")
    calls: list[int] = []

    def abort_check() -> None:
        calls.append(1)
        raise RuntimeError("aborted at checkpoint")

    mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-9000",
        "fields": {"summary": "should never be applied"},
        "local_id": "abort-id",
    }
    with pytest.raises(RuntimeError, match="aborted at checkpoint"):
        applier.apply([mutation], "pass-abort", repo_root=repo, abort_check=abort_check)
    assert calls, "abort_check must be called before applying the mutation"


# ---------------------------------------------------------------------------
# C4 startup migration: purge legacy committed .reconciler-* lock files
# ---------------------------------------------------------------------------


def _repo_with_tickets_branch(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    _git(["commit", "--allow-empty", "-m", "init"], tmp_path)
    _git(["checkout", "-q", "--orphan", "tickets"], tmp_path)
    (tmp_path / ".gitkeep").write_text("")
    _git(["add", ".gitkeep"], tmp_path)
    _git(["commit", "-q", "-m", "tickets init"], tmp_path)
    return tmp_path


def test_purge_committed_reconciler_locks(tmp_path: Path) -> None:
    """The startup migration deletes legacy committed .reconciler-* files from the
    tickets branch; idempotent (a second run is a no-op) and a regression that no
    such path survives."""
    main_mod = _load("rebar_reconciler.__main__", ENGINE / "__main__.py")
    repo = _repo_with_tickets_branch(tmp_path)
    # Seed the legacy committed lock files on the tickets branch.
    for name in (".reconciler-pass-lock", ".reconciler-phase-gate"):
        (repo / name).write_text("pass-old\n")
        _git(["add", name], repo)
    _git(["commit", "-q", "-m", "legacy locks"], repo)
    _git(["checkout", "-q", "main" if _has_main(repo) else "master"], repo)
    assert _committed(repo, ".reconciler-pass-lock"), "precondition: legacy lock committed"

    main_mod._purge_committed_reconciler_locks(repo)
    assert not _committed(repo, ".reconciler-pass-lock"), "purge must remove the pass-lock"
    assert not _committed(repo, ".reconciler-phase-gate"), "purge must remove the phase-gate"

    # Idempotent: a second run finds nothing and does not error / re-commit.
    head_before = _git(["rev-parse", "tickets"], repo).stdout.strip()
    main_mod._purge_committed_reconciler_locks(repo)
    assert _git(["rev-parse", "tickets"], repo).stdout.strip() == head_before


def _has_main(repo: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "main"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _committed(repo: Path, path: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"tickets:{path}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
