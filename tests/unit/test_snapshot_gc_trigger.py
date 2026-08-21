"""Bug ``undamaged-epidermic-kakarikis`` (58a3-0756-e470-4b40) — the operation-linked snapshot-GC
trigger.

The janitor's ONLY production driver was the review-bot's resident thread, so every other host
that resolves an attested gate populated ``$REBAR_GATE_TMPDIR/rebar-gate-snapshots`` and never
reclaimed it (measured: 47.24 GiB on one developer host). ``rebar._snapshot.gc_trigger``
mirrors the compaction sweep's operation-linked trigger (``compact_trigger.py``, the pattern
that fixed bug ``0d15-59a4``): a near-free stamp check on the tail of gate resolution, the
existing :func:`janitor.run_gc` policy in a DETACHED child, single-flight via a stamped worker
lock, and NO ticket-store lock anywhere in the trigger path.
"""

from __future__ import annotations

import os
import subprocess
import time
import types
from pathlib import Path

import pytest

from rebar import _proc
from rebar._snapshot import cache, gc_trigger, janitor
from rebar._snapshot import repo_snapshot as rs

try:
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None


# --------------------------------------------------------------------------------------
# fixtures (mirroring tests/unit/test_snapshot_janitor.py)
# --------------------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    base = tmp_path / "gate-tmpdir"
    base.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(base))
    return rs.store_root()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "repo")


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Capture detach requests instead of forking (compact_trigger test discipline)."""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        gc_trigger,
        "_spawn_detached_gc",
        lambda root, repo_root: calls.append((str(root), repo_root)),
    )
    return calls


def _populate_cold(repo: Path, store: Path, name: str) -> Path:
    """A real cache entry whose mtime is far past any max-age window."""
    sha = _commit(repo, name, name)
    cache.acquire(sha, repo_root=str(repo), fetch=False)
    entry = rs.entry_path(sha, store)
    old = time.time() - 10**7
    os.utime(entry, (old, old))
    return entry


# --------------------------------------------------------------------------------------
# (a) the operation-linked trigger fires when the stamp is due
# --------------------------------------------------------------------------------------
def test_fires_when_the_store_has_never_run_a_pass(
    store: Path, repo: Path, spawns: list[tuple[str, str | None]]
) -> None:
    """A missing stamp reads as due: the host that never reclaimed is the one that must."""
    gc_trigger.maybe_gc(repo_root=str(repo))
    assert spawns == [(str(store), str(repo))]


def test_quiet_on_a_fresh_stamp(
    store: Path, repo: Path, spawns: list[tuple[str, str | None]]
) -> None:
    gc_trigger.record_pass(store)
    gc_trigger.maybe_gc(repo_root=str(repo))
    assert spawns == []


def test_fires_again_once_the_stamp_goes_stale(
    store: Path, repo: Path, spawns: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REBAR_GATE_JANITOR_INTERVAL_SECONDS", "60")
    gc_trigger.record_pass(store)
    stamp = gc_trigger._stamp_path(store)
    old = time.time() - 3600
    os.utime(stamp, (old, old))
    gc_trigger.maybe_gc(repo_root=str(repo))
    assert spawns == [(str(store), str(repo))]


def test_interval_zero_disables_the_trigger(
    store: Path, repo: Path, spawns: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``interval_seconds <= 0`` is the off switch — no new knob, the janitor's own cadence."""
    monkeypatch.setenv("REBAR_GATE_JANITOR_INTERVAL_SECONDS", "0")
    gc_trigger.maybe_gc(repo_root=str(repo))
    assert spawns == []


def test_gate_resolution_invokes_the_trigger_for_an_attested_handle(
    store: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring: `resolve_gate_handle` — the choke point every attested gate op flows
    through — runs the trigger on its tail, and only for handles that POPULATE the store."""
    from rebar.llm import gate_source

    seen: list[str | None] = []
    monkeypatch.setattr(gc_trigger, "maybe_gc", lambda repo_root=None: seen.append(repo_root))

    handle = rs.SnapshotHandle(path=repo, sha="0" * 40, source=rs.SOURCE_ATTESTED)
    monkeypatch.setattr(gate_source, "acquire", lambda *a, **k: handle)
    monkeypatch.setattr(gate_source, "materialize_tickets", lambda **k: str(repo))
    monkeypatch.setattr(
        gate_source, "build_drift", types.SimpleNamespace(warn_if_behind=lambda *a, **k: None)
    )

    gate_source.resolve_gate_handle("origin/main", "attested", str(repo))
    assert seen == [str(repo)], "an attested gate resolution must run the GC trigger"

    seen.clear()
    local = rs.SnapshotHandle(path=repo, sha=None, source=rs.SOURCE_LOCAL)
    monkeypatch.setattr(gate_source, "acquire", lambda *a, **k: local)
    gate_source.resolve_gate_handle("origin/main", "local", str(repo))
    assert seen == [], "a local handle populates nothing, so it must not trigger GC"


def test_a_trigger_failure_never_reaches_the_gate(
    store: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never-raise posture: housekeeping must not fail the operation that triggered it."""

    def _boom(root: Path, repo_root: str | None) -> None:
        raise RuntimeError("detach exploded")

    monkeypatch.setattr(gc_trigger, "_spawn_detached_gc", _boom)
    gc_trigger.maybe_gc(repo_root=str(repo))  # must not raise


# --------------------------------------------------------------------------------------
# (b) the quiet check is O(1) and takes no ticket-store lock
# --------------------------------------------------------------------------------------
def test_the_quiet_check_never_enumerates_the_store(
    store: Path, repo: Path, spawns: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enrich-drain gate's marker discipline: the per-operation answer must be one marker
    ``stat``, not a
    walk that grows with the 64k-entry store this bug measured."""
    for i in range(25):
        (store / f"{i:040x}").mkdir()
    gc_trigger.record_pass(store)

    enumerated: list[str] = []
    real_scandir = os.scandir
    real_listdir = os.listdir

    def _spy_scandir(path=".", *a, **k):  # type: ignore[no-untyped-def]
        if str(path).startswith(str(store)):
            enumerated.append(str(path))
        return real_scandir(path, *a, **k)

    def _spy_listdir(path=".", *a, **k):  # type: ignore[no-untyped-def]
        if str(path).startswith(str(store)):
            enumerated.append(str(path))
        return real_listdir(path, *a, **k)

    monkeypatch.setattr(os, "scandir", _spy_scandir)
    monkeypatch.setattr(os, "listdir", _spy_listdir)

    gc_trigger.maybe_gc(repo_root=str(repo))

    assert spawns == []
    assert enumerated == [], f"the quiet check enumerated the store: {enumerated[:3]}"


def test_the_trigger_takes_no_ticket_store_lock(
    store: Path, repo: Path, spawns: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator ruling verbatim: the 0d15-59a4 pitfall was heavy work under the TICKET
    store lock. The trigger must not touch that lock in ANY branch — quiet or firing."""
    from rebar._store import lock as store_lock

    def _forbidden(*a: object, **k: object) -> None:
        raise AssertionError("the GC trigger touched the ticket-store lock")

    monkeypatch.setattr(store_lock, "acquire", _forbidden)
    monkeypatch.setattr(store_lock, "write_lock", _forbidden)

    gc_trigger.maybe_gc(repo_root=str(repo))  # due (no stamp) → fires
    assert len(spawns) == 1
    gc_trigger.record_pass(store)
    gc_trigger.maybe_gc(repo_root=str(repo))  # quiet
    assert len(spawns) == 1


# --------------------------------------------------------------------------------------
# (c) single-flight under contention
# --------------------------------------------------------------------------------------
def test_does_not_spawn_while_a_worker_holds_the_lock(
    store: Path, repo: Path, spawns: list[tuple[str, str | None]]
) -> None:
    fd = gc_trigger._acquire_worker_lock(store)
    assert fd is not None, "precondition: the first worker must acquire"
    try:
        gc_trigger.maybe_gc(repo_root=str(repo))
        assert spawns == [], "a due trigger spawned a second worker under a live lock"
    finally:
        gc_trigger.release_worker_lock(store, fd)


def test_second_acquire_loses_and_release_frees_the_path(store: Path) -> None:
    fd = gc_trigger._acquire_worker_lock(store)
    assert fd is not None
    try:
        assert gc_trigger._acquire_worker_lock(store) is None
    finally:
        gc_trigger.release_worker_lock(store, fd)
    assert not os.path.exists(gc_trigger._worker_lock_path(store))
    fd2 = gc_trigger._acquire_worker_lock(store)
    assert fd2 is not None
    gc_trigger.release_worker_lock(store, fd2)


def test_a_stale_worker_lock_is_reclaimed(store: Path) -> None:
    """A worker that died between acquire and release must not disable GC forever — the
    stamped-owner discipline inherited from the drain/compaction locks."""
    path = gc_trigger._worker_lock_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-a-v2-stamp")
    os.utime(path, (0, 0))

    fd = gc_trigger._acquire_worker_lock(store)
    assert fd is not None, "a provably-orphaned worker lock must be reclaimed, not respected"
    gc_trigger.release_worker_lock(store, fd)


@pytest.mark.skipif(fcntl is None, reason="requires fcntl")
def test_overlap_with_a_concurrent_run_gc_stands_aside_and_keeps_the_clock(
    store: Path, repo: Path
) -> None:
    """Double-running with the review-bot's resident janitor must be harmless: the child's
    ``run_gc`` degrades to ``skipped="locked"`` under the existing gc flock — and a skipped
    pass must NOT stamp (the ``run_sweep`` lesson: a stand-aside that resets the clock goes
    quiet forever under contention)."""
    entry = _populate_cold(repo, store, "cold.txt")
    lock_path = janitor._gc_lock_path(store)
    held = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gc_trigger.run_detached(str(store), str(repo))
    finally:
        os.close(held)

    assert entry.is_dir(), "a stand-aside must not evict"
    assert not gc_trigger._stamp_path(store).exists(), "a stand-aside reset the pass clock"


def test_a_run_gc_exception_neither_raises_nor_stamps(
    store: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child's other degraded path: a run_gc EXCEPTION (not a stand-aside) must be
    swallowed — the entries stay live — and must NOT stamp, or a persistently-failing pass
    would look freshly run and suppress the trigger for a full interval every time."""

    def _boom(*a: object, **k: object) -> janitor.GcResult:
        raise RuntimeError("gc pass exploded")

    monkeypatch.setattr(janitor, "run_gc", _boom)
    gc_trigger.run_detached(str(store), str(repo))  # must not raise

    assert not gc_trigger._stamp_path(store).exists(), "a failed pass reset the pass clock"
    assert not os.path.exists(gc_trigger._worker_lock_path(store)), (
        "a failed pass leaked the worker lock"
    )


# --------------------------------------------------------------------------------------
# the headline AC: reclamation on a simulated non-review-bot host
# --------------------------------------------------------------------------------------
def test_reclamation_happens_on_a_host_without_the_review_bot(
    store: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end minus the fork: no resident janitor anywhere, a cold entry, a due stamp —
    one gate-op-linked trigger reclaims the store, stamps the pass, and a second immediate
    trigger stays quiet (the failure/empty path)."""
    monkeypatch.setenv("REBAR_GATE_MAX_AGE_SECONDS", "3600")
    entry = _populate_cold(repo, store, "cold.txt")

    inline_runs: list[str] = []

    def _inline(root: Path, repo_root: str | None) -> None:
        inline_runs.append(str(root))
        gc_trigger.run_detached(str(root), repo_root)

    monkeypatch.setattr(gc_trigger, "_spawn_detached_gc", _inline)

    gc_trigger.maybe_gc(repo_root=str(repo))

    assert inline_runs == [str(store)]
    assert not entry.exists(), "the cold entry survived the operation-linked GC pass"
    assert gc_trigger._stamp_path(store).exists(), "an actual pass must stamp the clock"

    gc_trigger.maybe_gc(repo_root=str(repo))
    assert inline_runs == [str(store)], "a fresh stamp must keep the trigger quiet"


def test_run_detached_survives_a_dead_repo_root(store: Path, tmp_path: Path) -> None:
    """The detached child outlives the worktree that spawned it (bug ``3198-438c-72a5-470f``):
    a vanished
    ``repo_root`` degrades to default tunables, never to a crash."""
    gone = tmp_path / "vanished-worktree"
    gc_trigger.run_detached(str(store), str(gone))  # must not raise
    assert gc_trigger._stamp_path(store).exists()


# --------------------------------------------------------------------------------------
# the detach contract (argv capture, compact_trigger test style)
# --------------------------------------------------------------------------------------
def test_the_detached_child_is_anchored_to_the_durable_store_root(
    store: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is handed the resolved store root in argv, its cwd is the durable anchor the
    shared spawner derives from that same store root (per-host, outside any ephemeral
    worktree), and its stdio is detached."""
    seen: list[tuple[list[str], dict]] = []

    def _fake_popen(argv: list[str], **kwargs: object) -> object:
        seen.append((argv, kwargs))
        return object()

    # The one Popen lives in the shared spawner (rebar._proc.spawn_detached); patch that
    # module's OWN `subprocess` reference, never the real module.
    monkeypatch.setattr(
        _proc,
        "subprocess",
        types.SimpleNamespace(Popen=_fake_popen, DEVNULL=subprocess.DEVNULL),
    )
    gc_trigger._spawn_detached_gc(store, str(repo))

    assert seen, "precondition: a child was spawned"
    argv, kwargs = seen[0]
    assert str(store) in argv, f"the child was not handed the store root: {argv}"
    assert str(repo) in argv, f"the child was not handed the repo root: {argv}"
    assert kwargs.get("cwd") == _proc.detached_child_cwd(str(store)), (
        "the child's cwd must be the durable anchor derived from the store root"
    )
    assert kwargs.get("stdin") is subprocess.DEVNULL
    assert kwargs.get("start_new_session") is True
