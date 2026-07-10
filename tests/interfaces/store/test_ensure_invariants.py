"""WS5: authoritative behavioural invariants of the ensure-registry.

Owns the cross-cutting guarantees (the per-surface wiring is proven by WS1's
test_ensures.py, WS2's test_pending_hint.py, WS3's test_fsck_ensures.py): git-level
idempotency, the write hot-path budget (no sweep / no ensure-unit git-config /
≤1 marker read), real-multi-process concurrency under the store write lock, the
library read-only (no_mutate) guarantee, and absent/garbage marker degradation.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import rebar
from rebar._store import ensures


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.init_repo(repo_root=str(r))
    ensures._reset_pending_cache()
    return r


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _commit_count(tracker: Path) -> int:
    return int(
        subprocess.run(
            ["git", "-C", str(tracker), "rev-list", "--count", "tickets"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


# ── idempotency (git-log) ─────────────────────────────────────────────────────
def test_run_ensures_twice_zero_new_commits(repo: Path) -> None:
    tracker = _tracker(repo)
    # First sweep already ran at init; a converged store must produce zero commits.
    before = _commit_count(tracker)
    out1 = ensures.run_ensures(tracker)
    out2 = ensures.run_ensures(tracker)
    assert _commit_count(tracker) == before, "converged sweeps must not commit"
    assert all(o.status == "ok" for o in out1), out1
    assert all(o.status == "ok" for o in out2), out2


def test_run_ensures_converges_pending_then_idempotent(repo: Path) -> None:
    """A genuinely-behind store: strip the committed .gitignore blob so the gitignore
    unit must re-commit once, then a second sweep is a no-op (exactly one commit)."""
    tracker = _tracker(repo)
    # Force drift the gitignore unit will correct: remove the committed .gitignore.
    subprocess.run(["git", "-C", str(tracker), "rm", "-q", "--cached", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(tracker), "commit", "-q", "--no-verify", "-m", "drop gitignore"],
        check=True,
    )
    before = _commit_count(tracker)
    out1 = ensures.run_ensures(tracker)
    after1 = _commit_count(tracker)
    out2 = ensures.run_ensures(tracker)
    after2 = _commit_count(tracker)
    assert any(o.id == "gitignore" and o.status == "changed" for o in out1), out1
    assert after1 == before + 1, "drift correction is exactly one commit"
    assert after2 == after1, "second sweep on the reconverged store is a no-op"
    assert all(o.status == "ok" for o in out2), out2


# ── write hot-path budget ─────────────────────────────────────────────────────
def test_mutation_never_sweeps_and_reads_marker_once(repo: Path, monkeypatch) -> None:
    """(a) a ticket mutation never invokes run_ensures; (b) it reads .ensure-applied
    at most once per process (the cached pending set)."""
    swept = {"n": 0}
    monkeypatch.setattr(
        ensures, "run_ensures", lambda *a, **k: swept.__setitem__("n", swept["n"] + 1) or []
    )
    reads = {"n": 0}
    real_applied = ensures.applied_ids
    monkeypatch.setattr(
        ensures,
        "applied_ids",
        lambda t: (reads.__setitem__("n", reads["n"] + 1), real_applied(t))[1],
    )
    tid = rebar.create_ticket("task", "hot path", repo_root=str(repo))
    for i in range(5):
        rebar.comment(tid, f"write {i}", repo_root=str(repo))

    assert swept["n"] == 0, "a ticket mutation must never invoke run_ensures"
    assert reads["n"] <= 1, f".ensure-applied read {reads['n']}x; must be <=1 per process"


def test_pending_hint_spawns_no_subprocess(repo: Path, monkeypatch) -> None:
    """The write-path nudge only reads a marker file — it must never shell out
    (contrast the old init-time git-config migration). Spy subprocess around a
    direct call and assert zero invocations."""
    tracker = _tracker(repo)
    (tracker / ensures.APPLIED_MARKER).unlink(missing_ok=True)  # make it pending → hint fires
    ensures._reset_pending_cache()

    calls: list[object] = []
    for name in ("run", "Popen", "check_call", "check_output", "call"):
        real = getattr(subprocess, name)
        monkeypatch.setattr(
            subprocess,
            name,
            lambda *a, _r=real, **k: (calls.append(a[0] if a else None), _r(*a, **k))[1],
        )
    ensures.maybe_emit_pending_hint(tracker)
    assert calls == [], f"pending-hint spawned subprocess(es): {calls}"


# ── real multi-process concurrency ────────────────────────────────────────────
def test_concurrent_sweeps_serialize_and_write_atomic(repo: Path) -> None:
    tracker = _tracker(repo)
    # Make the store pending so both sweeps have work + write the marker.
    (tracker / ensures.APPLIED_MARKER).unlink(missing_ok=True)

    prog = textwrap.dedent(
        f"""
        import sys
        from rebar._store import ensures
        outs = ensures.run_ensures({str(tracker)!r})
        # Non-zero only on an outright crash; a skipped (lock-timeout) sweep is fine.
        sys.exit(0)
        """
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", prog], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(2)
    ]
    for p in procs:
        p.wait(timeout=120)
    assert all(p.returncode == 0 for p in procs), [p.returncode for p in procs]
    # The marker is intact (valid JSON, no torn/lost write) and fully converged.
    assert ensures.applied_ids(tracker) == set(ensures.REGISTRY_IDS)


# ── library read-only (no_mutate) never sweeps ────────────────────────────────
def test_no_mutate_fsck_never_sweeps(repo: Path, monkeypatch) -> None:
    tracker = _tracker(repo)
    (tracker / ensures.APPLIED_MARKER).unlink(missing_ok=True)
    before = _commit_count(tracker)

    swept = {"n": 0}
    monkeypatch.setattr(
        ensures, "run_ensures", lambda *a, **k: swept.__setitem__("n", swept["n"] + 1) or []
    )
    out = rebar.fsck(repo_root=str(repo))  # library read-only surface (no_mutate=True)

    assert swept["n"] == 0, "no_mutate read-only fsck must never sweep"
    assert not (tracker / ensures.APPLIED_MARKER).exists(), "read-only fsck wrote the marker"
    assert _commit_count(tracker) == before, "read-only fsck committed"
    assert "ensures:" in out  # still reports the informational line


# ── absent / garbage marker degradation ───────────────────────────────────────
def test_absent_and_garbage_marker_degrade_and_converge(repo: Path) -> None:
    tracker = _tracker(repo)
    marker = tracker / ensures.APPLIED_MARKER

    marker.unlink(missing_ok=True)
    assert ensures.applied_ids(tracker) == set()

    marker.write_text("}{ not json", encoding="utf-8")
    assert ensures.applied_ids(tracker) == set()

    marker.write_text('"a string, not a list"', encoding="utf-8")
    assert ensures.applied_ids(tracker) == set()

    # A garbage marker does not crash the sweep; it reconverges to a valid one.
    ensures._reset_pending_cache()
    ensures.run_ensures(tracker)
    assert ensures.applied_ids(tracker) == set(ensures.REGISTRY_IDS)
