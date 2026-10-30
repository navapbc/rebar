"""Commit-chain writes for ``refs/reconciler/*`` (metaphoric-late-creature, ADR 0031).

Every remote write is a plain fast-forward push of a commit whose parent is the tip the
writer read, pushed from a temporary named ref: git-remote-s3 cannot bundle a bare oid
and gives no compare on a forced push or a delete. Release and clear write markers that
rebar 0.13.x reads as an expired lease and an unknown (open) gate.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

from rebar.reconciler import _ref_lock as ref_lock
from rebar.reconciler import last_pass


def _git(args: list[str], repo: Path, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **kw
    )


def _bare(args: list[str], bare: Path) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(bare), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _clone(bare: Path, path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)
    _git(["remote", "add", "origin", str(bare)], path)
    return path


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, Path, Path]:
    bare = tmp_path / "bare.git"
    init_bare_remote(bare)
    return bare, _clone(bare, tmp_path / "a"), _clone(bare, tmp_path / "b")


def _tip(bare: Path, ref: str) -> str | None:
    out = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() or None


def _parent(bare: Path, oid: str) -> str | None:
    parents = _bare(["rev-list", "--parents", "-n", "1", oid], bare).split()[1:]
    return parents[0] if parents else None


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = ref_lock._git

    def _recording(repo_root, args, **kw):
        if args and args[0] == "push":
            seen.append(list(args))
        return real(repo_root, args, **kw)

    monkeypatch.setattr(ref_lock, "_git", _recording)
    return seen


def _lock_state(clone: Path) -> ref_lock.RefLockState | None:
    return ref_lock.read(clone, ref_lock.LOCK_REF, remote="origin")


def test_every_remote_write_is_a_named_ref_fast_forward(remote, pushes) -> None:
    _bare_dir, a, _b = remote
    oid = ref_lock.acquire(a, ref_lock.LOCK_REF, holder="p1", lease_secs=120, remote="origin")
    oid = ref_lock.renew(a, ref_lock.LOCK_REF, oid=oid, remote="origin")
    assert ref_lock.release(a, ref_lock.LOCK_REF, oid=oid, remote="origin") is True
    ref_lock.set_gate(a, "dry-run", remote="origin")
    assert ref_lock.clear_gate(a, remote="origin") is True
    assert pushes, "expected remote pushes"
    for argv in pushes:
        refspec = argv[-1]
        src = refspec.split(":", 1)[0]
        assert src.startswith("refs/reconciler/tmp/"), argv
        assert not refspec.startswith(("+", ":")), argv
        assert not any(a.startswith("--force") for a in argv), argv


def test_each_write_chains_onto_the_observed_tip(remote, tmp_path: Path) -> None:
    bare, a, b = remote
    lock = ref_lock.LOCK_REF
    first = ref_lock.acquire(a, lock, holder="p1", lease_secs=120, remote="origin")
    assert _bare(["cat-file", "-t", first], bare) == "commit"
    assert _parent(bare, first) is None, "acquire on an absent ref is a root commit"

    renewed = ref_lock.renew(a, lock, oid=first, remote="origin")
    assert _parent(bare, renewed) == first

    observed = _lock_state(b)
    stolen = ref_lock.try_break_if_stale(b, lock, first=observed, holder="p2", remote="origin")
    assert stolen is not None and _parent(bare, stolen) == renewed

    gate = ref_lock.set_gate(a, "dry-run", remote="origin")
    assert _parent(bare, gate) is None
    paused = ref_lock.set_pause(
        a, reason="r", who="w", paused_at="2026-10-30T00:00:00Z", remote="origin"
    )
    assert _parent(bare, paused) == gate


def test_last_pass_publish_chains(remote, monkeypatch: pytest.MonkeyPatch) -> None:
    bare, a, _b = remote
    monkeypatch.setattr(last_pass, "_remote", lambda _root: "origin")
    last_pass.publish(a, pass_id="p1", environment_id="e", outcome="success")
    first = _tip(bare, last_pass.LAST_PASS_REF)
    last_pass.publish(a, pass_id="p2", environment_id="e", outcome="success")
    second = _tip(bare, last_pass.LAST_PASS_REF)
    assert _parent(bare, second) == first
    assert json.loads(_bare(["cat-file", "blob", f"{second}:lock.json"], bare))["pass_id"] == "p2"


def _race(fns) -> list[object]:
    results: list[object] = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def _run(i, fn):
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - the race outcome is the assertion
            results[i] = exc

    threads = [threading.Thread(target=_run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _acquirers(clones: list[Path]):
    return [
        (
            lambda c=c: ref_lock.acquire(
                c, ref_lock.LOCK_REF, holder=c.name, lease_secs=120, remote="origin"
            )
        )
        for c in clones
    ]


def test_racing_acquire_on_an_absent_ref_has_one_winner(remote) -> None:
    _bare_dir, a, b = remote
    results = _race(_acquirers([a, b]))
    winners = [r for r in results if isinstance(r, str)]
    assert len(winners) == 1, results
    assert any(isinstance(r, ref_lock.RefLockHeldError) for r in results), results


def test_racing_acquire_on_a_released_marker_has_one_winner(remote) -> None:
    _bare_dir, a, b = remote
    oid = ref_lock.acquire(a, ref_lock.LOCK_REF, holder="p0", lease_secs=120, remote="origin")
    assert ref_lock.release(a, ref_lock.LOCK_REF, oid=oid, remote="origin") is True
    results = _race(_acquirers([a, b]))
    assert len([r for r in results if isinstance(r, str)]) == 1, results


def test_renew_losing_to_a_steal_raises_lease_lost(remote) -> None:
    _bare_dir, a, b = remote
    oid = ref_lock.acquire(a, ref_lock.LOCK_REF, holder="p1", lease_secs=120, remote="origin")
    observed = _lock_state(b)
    assert ref_lock.try_break_if_stale(
        b, ref_lock.LOCK_REF, first=observed, holder="p2", remote="origin"
    )
    with pytest.raises(ref_lock.LeaseLostError):
        ref_lock.renew(a, ref_lock.LOCK_REF, oid=oid, remote="origin")
    assert _lock_state(a).holder == "p2"


def test_release_losing_to_a_steal_returns_false(remote) -> None:
    _bare_dir, a, b = remote
    oid = ref_lock.acquire(a, ref_lock.LOCK_REF, holder="p1", lease_secs=120, remote="origin")
    observed = _lock_state(b)
    assert ref_lock.try_break_if_stale(
        b, ref_lock.LOCK_REF, first=observed, holder="p2", remote="origin"
    )
    assert ref_lock.release(a, ref_lock.LOCK_REF, oid=oid, remote="origin") is False
    assert _lock_state(b).holder == "p2"


def test_released_lock_reads_free_and_decodes_as_an_expired_lease(remote) -> None:
    bare, a, _b = remote
    oid = ref_lock.acquire(a, ref_lock.LOCK_REF, holder="p1", lease_secs=120, remote="origin")
    assert ref_lock.release(a, ref_lock.LOCK_REF, oid=oid, remote="origin") is True
    assert _lock_state(a) is None
    raw = _bare(["cat-file", "blob", f"{ref_lock.LOCK_REF}:lock.json"], bare).encode() + b"\n"
    # _decode_blob is byte-identical to v0.13.1's, so this is how 0.13.x reads the marker.
    legacy = ref_lock._decode_blob(raw, "x" * 40)
    assert legacy.lease_secs == 1
    assert legacy.fence == 1


def test_cleared_gate_reads_open(remote) -> None:
    bare, a, _b = remote
    ref_lock.set_pause(a, reason="r", who="w", paused_at="2026-10-30T00:00:00Z", remote="origin")
    assert ref_lock.clear_gate(a, remote="origin") is True
    assert ref_lock.read_gate(a, remote="origin") is None
    assert ref_lock.read_pause(a, remote="origin") is None
    marker = json.loads(_bare(["cat-file", "blob", f"{ref_lock.GATE_REF}:gate.json"], bare))
    assert marker["gated_mode"] not in {"dry-run", "reconcile-check", "live", "bootstrap"}


def test_clear_gate_reports_whether_it_changed_anything(remote) -> None:
    _bare_dir, a, _b = remote
    assert ref_lock.clear_gate(a, remote="origin") is False
    ref_lock.set_gate(a, "dry-run", remote="origin")
    assert ref_lock.clear_gate(a, remote="origin") is True
    assert ref_lock.clear_gate(a, remote="origin") is False


def _plant_legacy_blob(bare: Path, ref: str, payload: dict) -> str:
    oid = (
        subprocess.run(
            ["git", "--git-dir", str(bare), "hash-object", "-w", "--stdin"],
            input=(json.dumps(payload, sort_keys=True) + "\n").encode(),
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    _bare(["update-ref", ref, oid], bare)
    return oid


def test_writes_over_a_legacy_blob_tip_leave_a_commit(remote) -> None:
    bare, a, _b = remote
    legacy = _plant_legacy_blob(
        bare,
        ref_lock.LOCK_REF,
        {"holder": "p1", "lease_secs": 120, "heartbeat_ns": 1, "fence": 0},
    )
    renewed = ref_lock.renew(a, ref_lock.LOCK_REF, oid=legacy, remote="origin")
    assert _bare(["cat-file", "-t", renewed], bare) == "commit"
    assert _lock_state(a).fence == 1

    _plant_legacy_blob(bare, ref_lock.GATE_REF, {"gated_mode": "dry-run"})
    gate = ref_lock.set_gate(a, "live", remote="origin")
    assert _bare(["cat-file", "-t", gate], bare) == "commit"
    assert ref_lock.read_gate(a, remote="origin") == "live"


def _fail_pushes(monkeypatch: pytest.MonkeyPatch, stderr: str, before=None) -> None:
    real = ref_lock._git

    def _git(repo_root, args, **kw):
        if args and args[0] == "push":
            if before is not None:
                before()
            return subprocess.CompletedProcess(["git", *args], 1, "", stderr)
        return real(repo_root, args, **kw)

    monkeypatch.setattr(ref_lock, "_git", _git)


def test_rejection_with_the_tip_unchanged_surfaces_the_remote_error(
    remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bare_dir, a, _b = remote
    _fail_pushes(monkeypatch, "fatal: Refusing to create empty bundle.")
    with pytest.raises(ref_lock.RefLockError) as raised:
        ref_lock.acquire(a, ref_lock.LOCK_REF, holder="p1", lease_secs=120, remote="origin")
    assert "Refusing to create empty bundle" in str(raised.value)
    assert not isinstance(raised.value, ref_lock.RefLockHeldError)


def test_rejection_after_the_tip_moved_is_a_cas_mismatch(
    remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bare_dir, a, b = remote

    def _someone_else_wins() -> None:
        monkeypatch.undo()
        ref_lock.acquire(b, ref_lock.LOCK_REF, holder="p2", lease_secs=120, remote="origin")

    _fail_pushes(monkeypatch, "stale remote. Please fetch and retry.", before=_someone_else_wins)
    with pytest.raises(ref_lock.RefLockHeldError):
        ref_lock.acquire(a, ref_lock.LOCK_REF, holder="p1", lease_secs=120, remote="origin")


def test_local_writes_chain_without_a_remote(tmp_path: Path) -> None:
    repo = _clone(tmp_path / "unused.git", tmp_path / "solo")
    oid = ref_lock.acquire(repo, ref_lock.LOCK_REF, holder="p1", lease_secs=120)
    renewed = ref_lock.renew(repo, ref_lock.LOCK_REF, oid=oid)
    assert _git(["rev-parse", f"{renewed}^"], repo).stdout.strip() == oid
    assert ref_lock.release(repo, ref_lock.LOCK_REF, oid=renewed) is True
    assert ref_lock.read(repo, ref_lock.LOCK_REF) is None
    assert ref_lock.acquire(repo, ref_lock.LOCK_REF, holder="p2", lease_secs=120)
