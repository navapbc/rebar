"""The operation-linked compaction trigger — the floor for stores with no CI and no cron
(story gaudy-gangrenous-basilisk).

THE GAP THIS CLOSES. Compaction left the close path because it held the store write lock for
minutes (bug choosy-arthrodic-barbet), and a scheduled GitHub Actions sweep replaced it. But a
schedule needs CI or cron, and an adopter running rebar as a library or CLI has neither — for
them a CI-only trigger is no trigger at all, and their event logs would grow forever. So the
close TRIGGERS compaction again without PERFORMING it: two O(1) checks on the tail of the
close, and any real folding handed to a detached worker.

What is asserted here, in the order it matters:

* the trigger fires on the two conditions that make it a real floor — the just-closed ticket
  needs folding, OR the last sweep is stale — and stays quiet otherwise;
* the staleness arm is not decorative: it is what covers a store whose closed tickets happen
  never to be foldable;
* it does not storm — a held advisory lock suppresses the spawn, and a provably-orphaned lock
  is reclaimed rather than disabling the trigger forever;
* the close itself holds NO store lock across any of it, which is the whole reason compaction
  was moved in the first place.

The spawn is asserted through a stubbed `_spawn_detached_sweep`: a real detached child would
outlive the assertion, and what this module owns is the DECISION to spawn, not the child.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact_trigger

pytestmark = pytest.mark.unit

_HOUR_NS = 3_600_000_000_000


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> str:
    return str(rebar.config.tracker_dir(str(repo)))


def _seed(repo: Path, title: str, comments: int) -> str:
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return rebar._engine_support.resolver.resolve_ticket_id(tid, _tracker(repo))


def _age_events(tdir: Path, by_ns: int) -> None:
    import json

    for path in sorted(tdir.glob("*.json")):
        if path.name.startswith("."):
            continue
        event = json.loads(path.read_text())
        ts = event.get("timestamp")
        if not isinstance(ts, int):
            continue
        event["timestamp"] = ts - by_ns
        rest = path.name.split("-", 1)[1]
        path.write_text(json.dumps(event))
        path.rename(path.parent / f"{event['timestamp']}-{rest}")


@pytest.fixture(autouse=True)
def _trigger_async_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """This module is where the trigger is exercised, so opt back IN to the production default.

    The suite-wide conftest fixture forces `REBAR_COMPACT_TRIGGER=off` so no other test spawns
    a detached worker that would race the next test's store. These tests need the real default
    (`async`), and set `off`/`always` explicitly where that is the thing under test.

    Set UNCONDITIONALLY: the conftest fixture has already put `off` in the environment by the
    time this runs, so a "only if unset" guard would never fire. A test that wants a different
    mode calls `monkeypatch.setenv` in its own body, which runs after every fixture and wins."""
    monkeypatch.setenv("REBAR_COMPACT_TRIGGER", "async")


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record spawn decisions instead of detaching a child that would outlive the test."""
    calls: list[str] = []
    monkeypatch.setattr(compact_trigger, "_spawn_detached_sweep", lambda t: calls.append(t))
    return calls


# ── the trigger fires when the just-closed ticket needs folding ──────────────────────────────
def test_fires_when_the_written_ticket_needs_folding(
    store: Path, monkeypatch: pytest.MonkeyPatch, spawns: list[str]
) -> None:
    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    tid = _seed(repo, "needs folding", comments=4)
    tracker = _tracker(repo)
    _age_events(Path(tracker) / tid, _HOUR_NS)
    compact_trigger.record_sweep(tracker)  # a fresh sweep, so ONLY the ticket arm can fire

    compact_trigger.maybe_compact(tracker, tid, repo_root=str(repo))

    assert spawns, "a foldable just-closed ticket must trigger an out-of-band sweep"


def test_quiet_when_neither_condition_holds(
    store: Path, monkeypatch: pytest.MonkeyPatch, spawns: list[str]
) -> None:
    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "9999")
    tid = _seed(repo, "nothing to do", comments=1)
    tracker = _tracker(repo)
    # Give it a SNAPSHOT so the backfill arm cannot fire, and a fresh sweep stamp.
    from rebar._commands import compact as _compact

    _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo))
    _age_events(Path(tracker) / tid, _HOUR_NS)
    compact_trigger.record_sweep(tracker)

    compact_trigger.maybe_compact(tracker, tid, repo_root=str(repo))

    assert not spawns, "the trigger fired with nothing to fold and a fresh sweep"


# ── the staleness arm is what makes it a floor ───────────────────────────────────────────────
def test_fires_on_a_stale_sweep_even_when_the_ticket_is_fine(
    store: Path, monkeypatch: pytest.MonkeyPatch, spawns: list[str]
) -> None:
    """Without this arm the floor has a hole: a store whose closed tickets happen never to be
    foldable would never fold the ones that are."""
    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "9999")
    tid = _seed(repo, "fine itself", comments=1)
    tracker = _tracker(repo)
    from rebar._commands import compact as _compact

    _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo))
    assert not compact_trigger.ticket_needs_folding(tracker, tid), "precondition: ticket is fine"

    # A sweep stamp far in the past.
    compact_trigger.record_sweep(tracker)
    stamp = compact_trigger._sweep_stamp_path(tracker)
    os.utime(stamp, (0, 0))

    compact_trigger.maybe_compact(tracker, tid, repo_root=str(repo))

    assert spawns, "a stale sweep must trigger even when the written ticket needs nothing"


def test_a_store_that_never_swept_reads_as_stale(store: Path) -> None:
    """A missing stamp is the store that most needs a sweep, so it must not read as fresh."""
    tracker = _tracker(store)
    assert compact_trigger._sweep_is_stale(tracker, 3600) is True


def test_interval_zero_disables_the_staleness_arm(store: Path) -> None:
    tracker = _tracker(store)
    assert compact_trigger._sweep_is_stale(tracker, 0) is False


# ── storm control ────────────────────────────────────────────────────────────────────────────
def test_does_not_spawn_while_a_worker_holds_the_lock(
    store: Path, monkeypatch: pytest.MonkeyPatch, spawns: list[str]
) -> None:
    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    tid = _seed(repo, "burst of closes", comments=4)
    tracker = _tracker(repo)
    _age_events(Path(tracker) / tid, _HOUR_NS)

    held = compact_trigger._acquire_trigger_lock(tracker)
    assert held is not None, "precondition: the lock was free"
    try:
        compact_trigger.maybe_compact(tracker, tid, repo_root=str(repo))
    finally:
        compact_trigger.release_trigger_lock(tracker, held)

    assert not spawns, "a burst of closes must not spawn a burst of compactors"


def test_a_stale_worker_lock_is_reclaimed(store: Path) -> None:
    """A worker that died between acquire and release must not disable the trigger forever —
    the exact failure the drain lock had to be fixed for."""
    tracker = _tracker(store)
    os.makedirs(compact_trigger._rebar_dir(tracker), exist_ok=True)
    path = compact_trigger._trigger_lock_path(tracker)
    # An unrecognised (pre-stamp shaped) lock, aged past the shared wall-clock ceiling.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("not-a-v2-stamp")
    os.utime(path, (0, 0))

    fd = compact_trigger._acquire_trigger_lock(tracker)

    assert fd is not None, "a provably-orphaned worker lock must be reclaimed, not respected"
    compact_trigger.release_trigger_lock(tracker, fd)


def test_lock_round_trip_frees_the_path(store: Path) -> None:
    tracker = _tracker(store)
    fd = compact_trigger._acquire_trigger_lock(tracker)
    assert fd is not None
    compact_trigger.release_trigger_lock(tracker, fd)
    assert not os.path.exists(compact_trigger._trigger_lock_path(tracker))


# ── the knob ─────────────────────────────────────────────────────────────────────────────────
def test_off_never_triggers(
    store: Path, monkeypatch: pytest.MonkeyPatch, spawns: list[str]
) -> None:
    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_COMPACT_TRIGGER", "off")
    tid = _seed(repo, "operator drives compaction", comments=4)
    tracker = _tracker(repo)
    _age_events(Path(tracker) / tid, _HOUR_NS)
    # Guard against a vacuous pass: `maybe_compact` also returns quietly when the config cannot
    # be read, so assert the knob genuinely resolved to "off" before trusting the absence of a
    # spawn. Without this the test would still pass if the env wiring silently broke.
    assert rebar.config.load_config(str(repo)).compact.trigger == "off"
    # And the setup would otherwise definitely fire: no sweep stamp exists, so the staleness
    # arm is live on top of the ticket arm.
    assert compact_trigger.ticket_needs_folding(tracker, tid)

    compact_trigger.maybe_compact(tracker, tid, repo_root=str(repo))

    assert not spawns, "trigger=off must not compact"


def test_always_folds_inline(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`always` is what lets the suite exercise the fold synchronously — a detached child would
    outlive the assertion."""
    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_COMPACT_TRIGGER", "always")
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    tid = _seed(repo, "fold me inline", comments=4)
    tracker = _tracker(repo)

    compact_trigger.maybe_compact(tracker, tid, repo_root=str(repo))

    assert list((Path(tracker) / tid).glob("*-SNAPSHOT.json")), (
        "trigger=always must fold inline, so the fold is observable in-process"
    )


# ── the close holds no store lock across the trigger ─────────────────────────────────────────
def test_the_close_neither_folds_nor_holds_the_lock(
    store: Path, monkeypatch: pytest.MonkeyPatch, spawns: list[str]
) -> None:
    """The whole point of moving compaction: a close must hand the work off, not do it.

    Exercised in `async`, the PRODUCTION mode — and that distinction is the correction to an
    earlier version of this test, which used `always` and asserted the lock was never held
    during a fold. That assertion was false by construction: `always` exists precisely to run
    the fold INLINE so the suite can observe it, so of course the folding process holds the
    store lock. Testing the production claim against the test-only mode proved nothing and
    failed as soon as the trigger actually started firing.

    Two observations, both structural rather than timed: across a real close the closing
    process writes no fold artifact at all, and the lock dir is never seen while one exists.
    The spawn is stubbed because a real detached child would outlive the assertion — what this
    asserts is that the close DELEGATED, which is exactly the property under test."""
    from rebar._commands import gates as _gates
    from rebar._commands import transition as _transition

    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setattr(_gates, "gate_enabled", lambda root, key, **k: False)

    tid = _seed(repo, "close with trigger armed", comments=3)
    rebar.claim(tid, repo_root=str(repo))
    tracker = _tracker(repo)
    tdir = Path(tracker) / tid
    lock_dir = Path(tracker) / ".ticket-write.lock.d"

    locked_during_fold: list[str] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            if lock_dir.exists():
                folded = sorted(p.name for p in tdir.glob("*-SNAPSHOT.json"))
                if folded:
                    locked_during_fold.append(",".join(folded))
            stop.wait(0.02)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        _transition.transition_compute(tid, "in_progress", "closed", repo_root=str(repo))
    finally:
        stop.set()
        sampler.join()

    assert spawns, "the close must hand the fold to an out-of-band worker"
    assert not list(tdir.glob("*-SNAPSHOT.json")), (
        "the close folded inline — compaction must be delegated, not performed"
    )
    assert not locked_during_fold, (
        "the store write lock was held while a fold artifact existed "
        f"(saw: {locked_during_fold[0]})"
    )
    assert not lock_dir.exists(), "the close left the store write lock held"


# ── the worker yields to foreground writers ──────────────────────────────────────────────────
def test_the_worker_stands_aside_while_a_foreground_writer_holds_the_lock(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compaction is optional housekeeping and must never compete with work someone is waiting
    on. This guard is the bug-7084 probe, which could never fire where it was born — inline on
    the close path the closing process had just released the lock, so its own probe always read
    "free". Out of band it measures another SESSION's activity, because the compactor is a
    different process from the writers it might obstruct.

    Proven observationally: with the store lock held, the sweep writes no SNAPSHOT.
    """
    from rebar._store import lock as _lock

    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    tid = _seed(repo, "someone else is writing", comments=4)
    tracker = _tracker(repo)

    handle = _lock.acquire(tracker, timeout=5, attempts=1, dual_window=True)
    try:
        compact_trigger.run_sweep(tracker)
    finally:
        handle.release()

    assert not list((Path(tracker) / tid).glob("*-SNAPSHOT.json")), (
        "the worker folded while a foreground writer held the store lock — optional "
        "housekeeping must yield, not compete"
    )


def test_the_worker_folds_when_the_store_is_free(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control for the stand-aside: same setup, lock NOT held, work happens. Without
    this the test above would pass even if the sweep were broken outright."""
    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    tid = _seed(repo, "store is quiet", comments=4)
    tracker = _tracker(repo)

    compact_trigger.run_sweep(tracker)

    assert list((Path(tracker) / tid).glob("*-SNAPSHOT.json")), (
        "the worker did not fold an eligible ticket on a free store"
    )


def test_a_stand_aside_does_not_reset_the_sweep_clock(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that stood aside swept NOTHING, so it must not touch the last-sweep stamp.

    This is a hole straight through the floor if you get it wrong, and it is easy to get wrong:
    stamping in a `finally` looks like tidy bookkeeping. But the stamp gates the staleness arm,
    so a stand-aside that stamps suppresses the trigger for a whole interval — and under
    sustained contention EVERY trigger stands aside, stamps, and goes quiet. The store would
    then never compact while looking freshly swept, which is worse than not having the floor at
    all, because the signal says it is working.
    """
    from rebar._store import lock as _lock

    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    _seed(repo, "contended", comments=4)
    tracker = _tracker(repo)
    stamp = compact_trigger._sweep_stamp_path(tracker)
    assert not os.path.exists(stamp), "precondition: never swept"

    handle = _lock.acquire(tracker, timeout=5, attempts=1, dual_window=True)
    try:
        compact_trigger.run_sweep(tracker)
    finally:
        handle.release()

    assert not os.path.exists(stamp), (
        "standing aside stamped the sweep clock — the staleness arm is now suppressed for a "
        "full interval by a sweep that folded nothing"
    )
    # And the clock still being unset means the very next close retries, which is the point.
    assert compact_trigger._sweep_is_stale(tracker, 3600) is True
