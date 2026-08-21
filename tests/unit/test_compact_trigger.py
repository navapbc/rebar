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
import types
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


# ── one store, one set of sidecars (bug intangible-ladyish-vicuna 93a9-66cf-e681-4f49) ───────
#
# `make worktree` provisions a worktree whose `.tickets-tracker` is a SYMLINK to the canonical
# store while its `.rebar` is a real, per-worktree directory. The trigger derived all three of
# its sidecars from `os.path.dirname(tracker)` without resolving that symlink, so each worktree
# keyed its own worker lock, its own sweep stamp and its own log while triggering sweeps of the
# ONE shared store. Observed on this host: 33 worktree-local stamps and logs, no shared lock.
#
# The store's own contract is explicit — `_store.lock.canonical_tracker` exists "so symlinked
# and real-path callers contend on the SAME lock file", and the neighbouring `.rebar/hlc.lock`
# already resolves internally. These tests pin the trigger to that contract, artifact by
# artifact, because the three fail differently: the lock loses exclusion, the stamp makes every
# fresh worktree re-fire a sweep the store just had, and the log is deleted with the worktree.


def _canonical_store(tmp_path: Path) -> str:
    """A canonical store: ``<root>/.tickets-tracker``. Returns the tracker path."""
    tracker = Path(os.path.realpath(tmp_path)) / "canonical-repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    return str(tracker)


def _worktree_tracker(tmp_path: Path, tracker: str, name: str) -> str:
    """A worktree view of *tracker*: a real ``.rebar`` beside a ``.tickets-tracker`` SYMLINK
    into the canonical store, exactly as ``make worktree`` provisions one."""
    wt = Path(os.path.realpath(tmp_path)) / name
    (wt / ".rebar").mkdir(parents=True)
    (wt / ".tickets-tracker").symlink_to(tracker)
    return str(wt / ".tickets-tracker")


def test_two_worktrees_of_one_store_derive_one_set_of_sidecar_paths(tmp_path: Path) -> None:
    """Independence from the caller proved by INVARIANCE: two worktree views of one store must
    derive the SAME sidecar paths, and they must be the canonical store's rather than either
    worktree's. A sidecar keyed on the caller is as short-lived as the caller."""
    canonical = _canonical_store(tmp_path)
    a = _worktree_tracker(tmp_path, canonical, "worktree-a")
    b = _worktree_tracker(tmp_path, canonical, "worktree-b")

    for derive in (
        compact_trigger._trigger_lock_path,
        compact_trigger._sweep_stamp_path,
        compact_trigger._trigger_log_path,
    ):
        assert derive(a) == derive(b), f"{derive.__name__} is keyed on the worktree"
        assert derive(a) == derive(canonical), f"{derive.__name__} is not on the canonical store"


def test_two_worktrees_of_one_store_contend_on_the_same_worker_lock(tmp_path: Path) -> None:
    """The lock's stated intent — "one compactor at a time" — must hold ACROSS worktrees.

    Path equality above is necessary but not sufficient: this drives the real acquire, so a fix
    that renamed a path without restoring exclusion still fails here."""
    canonical = _canonical_store(tmp_path)
    a = _worktree_tracker(tmp_path, canonical, "worktree-a")
    b = _worktree_tracker(tmp_path, canonical, "worktree-b")

    held = compact_trigger._acquire_trigger_lock(a)
    assert held is not None, "precondition: the first compactor must acquire"
    try:
        assert compact_trigger._acquire_trigger_lock(b) is None, (
            "a second compactor on the SAME store acquired the worker lock concurrently"
        )
    finally:
        compact_trigger.release_trigger_lock(a, held)


def test_a_sweep_stamped_from_one_worktree_is_read_by_another(tmp_path: Path) -> None:
    """The stamp's own behavioural consequence, which the lock does not share: it answers "does
    this STORE need a sweep". Keyed on the worktree, every fresh worktree reads maximally stale
    and re-fires a store-wide sweep the store just had."""
    canonical = _canonical_store(tmp_path)
    a = _worktree_tracker(tmp_path, canonical, "worktree-a")
    b = _worktree_tracker(tmp_path, canonical, "worktree-b")

    # Negative control: before any sweep, B must read stale — otherwise the assertion below
    # could pass on a store that simply never reads the stamp at all.
    assert compact_trigger._sweep_is_stale(b, 3600) is True, "precondition: never swept"

    compact_trigger.record_sweep(a)

    assert compact_trigger._sweep_is_stale(b, 3600) is False, (
        "a worktree re-fired a store-wide sweep that another worktree had just run"
    )


def test_the_trigger_sidecars_are_never_written_inside_an_ephemeral_worktree(
    tmp_path: Path,
) -> None:
    """The durability half: the artifacts must land on the store, so they survive the worktree.

    Negative control for the two tests above — exclusion and stamp-sharing could in principle be
    restored by keying every worktree on the FIRST one, which would still be deleted with that
    worktree."""
    canonical = _canonical_store(tmp_path)
    wt = _worktree_tracker(tmp_path, canonical, "worktree-a")
    canonical_rebar = os.path.join(os.path.dirname(canonical), ".rebar")

    fd = compact_trigger._acquire_trigger_lock(wt)
    assert fd is not None
    try:
        compact_trigger.record_sweep(wt)
        assert os.listdir(os.path.join(os.path.dirname(wt), ".rebar")) == [], (
            "the trigger wrote sidecars into the worktree that spawned it"
        )
        assert sorted(os.listdir(canonical_rebar)) == [
            "compact-sweep.stamp",
            "compact-worker.lock",
        ]
    finally:
        compact_trigger.release_trigger_lock(wt, fd)


def test_a_detached_sweep_child_is_handed_the_canonical_store_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child outlives the worktree that spawned it, so the tracker it is handed must outlive
    that worktree too — otherwise it dies on a symlink into a directory that no longer exists.
    Its ``cwd`` is already resolved (``_proc.detached_child_cwd``); its ARGUMENT was not."""
    canonical = _canonical_store(tmp_path)
    wt = _worktree_tracker(tmp_path, canonical, "worktree-a")
    seen: list[list[str]] = []

    def _fake_popen(argv: list[str], **kwargs: object) -> object:
        seen.append(argv)
        return object()

    # Patch the spawn owner's OWN `subprocess` reference (`_proc` holds the one Popen since
    # task 2dc4-9bcd-75b9-4544), never the real module: a global patch outlives this test's
    # body and breaks `subprocess.run` in fixture teardown.
    from rebar import _proc

    monkeypatch.setattr(
        _proc,
        "subprocess",
        types.SimpleNamespace(Popen=_fake_popen, DEVNULL=subprocess.DEVNULL),
    )
    compact_trigger._spawn_detached_sweep(wt)

    assert seen, "precondition: a child was spawned"
    assert canonical in seen[0], (
        f"the detached sweep was handed the ephemeral worktree tracker: {seen[0]}"
    )
