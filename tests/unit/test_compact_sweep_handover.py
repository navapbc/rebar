"""The compaction sweep hands the store write lock to a waiting writer between folds, and
publishes each fold before starting the next (bug dc5a-5ca5-efca-4145).

The store lock keeps no queue: a waiter re-polls on a short jittered interval and sleeps
between passes. A sweep that releases, probes and re-takes within microseconds never lets a
waiter in, and its probe never sees one, so a long sweep starved canonical writes until they
timed out. The cross-process test below drives the real sweep loop against the real lock with
a writer in another process.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

import rebar
from rebar._commands import compact as _compact
from rebar._store import lock as _lock

pytestmark = pytest.mark.unit

# The writer subprocess: report readiness after its imports, start waiting for the lock only
# when told to (from inside a fold, so the sweep already holds it), with a short single-pass
# budget, and hold the lock long enough for the sweep's probe to see it.
_WRITER_SRC = """
import sys, time
from rebar._store import lock
tracker, budget, hold = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
sys.stdout.write("ready\\n"); sys.stdout.flush()
sys.stdin.readline()
t0 = time.monotonic()
try:
    handle = lock.acquire(tracker, timeout=budget, attempts=1)
except lock.LockTimeout:
    sys.stdout.write("timeout %.2f\\n" % (time.monotonic() - t0)); sys.stdout.flush()
    sys.exit(3)
sys.stdout.write("got %.2f\\n" % (time.monotonic() - t0)); sys.stdout.flush()
time.sleep(hold)
handle.release()
"""

_FOLD_HOLD_S = 0.3
_WRITER_BUDGET_S = 2
_TICKETS = 20  # 20 folds x 0.3 s of holding outlasts the writer's 2 s budget several times


def _sweep_like_fold(on_first_hold):
    """Stand-in for one locked fold: take the real store lock and hold it briefly. The first
    fold calls *on_first_hold* while it holds the lock."""
    calls = {"n": 0}

    def fold(tracker, tid, repo_root, position_commits, *, no_commit=False) -> str:
        with _lock.write_lock(tracker, timeout=10, attempts=1):
            calls["n"] += 1
            if calls["n"] == 1:
                on_first_hold()
            time.sleep(_FOLD_HOLD_S)
        return "."

    return fold


def test_a_writer_waiting_in_another_process_gets_the_lock_between_folds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = str(tmp_path / "store")
    Path(tracker).mkdir()
    writer = subprocess.Popen(
        [sys.executable, "-c", _WRITER_SRC, tracker, str(_WRITER_BUDGET_S), "1.5"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def release_the_writer() -> None:
        assert writer.stdin is not None
        writer.stdin.write("go\n")
        writer.stdin.flush()

    monkeypatch.setattr(_compact, "_sweep_position_map", lambda _repo_root: None)
    monkeypatch.setattr(_compact, "_sweep_one_ticket", _sweep_like_fold(release_the_writer))
    try:
        assert writer.stdout is not None
        assert writer.stdout.readline().strip() == "ready"
        compacted, _skipped, _ids, _errors = _compact._run_sweep(
            tracker, [f"t{n}" for n in range(_TICKETS)], None
        )
        out, err = writer.communicate(timeout=30)
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.wait()

    assert out.startswith("got"), (
        f"the writer starved behind the sweep (rc={writer.returncode}): {out!r} {err[-500:]!r}"
    )
    assert compacted < _TICKETS, "the sweep never stood aside for the writer holding the lock"


def test_the_sweep_pauses_after_every_fold_and_never_before_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps: list[str] = []
    monkeypatch.setattr(_compact, "_sweep_position_map", lambda _repo_root: None)
    monkeypatch.setattr(_compact, "_sweep_one_ticket", lambda *a, **k: steps.append("fold") or ".")
    monkeypatch.setattr(_compact, "_pause", lambda s: steps.append("pause"))
    monkeypatch.setattr(
        _compact, "_sweep_should_yield", lambda t, done: steps.append("probe") and False
    )

    _compact._run_sweep(str(tmp_path), ["a", "b", "c"], None)

    assert steps == ["probe", "fold", "pause", "probe", "fold", "pause", "probe", "fold"]


def test_a_short_fold_pause_outlasts_a_waiters_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paused: list[float] = []
    monkeypatch.setattr(_compact, "_pause", paused.append)

    _compact._give_waiters_a_turn(0.2)

    assert paused[0] > _lock.max_waiter_gap_s(between_passes=False)
    assert paused[0] < _lock.max_waiter_gap_s(between_passes=True), (
        "a fold too short to outlast any writer's pass must not pay the backoff-length pause"
    )


def test_a_long_fold_pause_outlasts_a_waiters_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a fold of a second or more, a waiter may have exhausted a pass and be asleep in
    retry backoff; the pause must outlast the longest backoff ``acquire`` can actually sleep."""
    import random

    # Drive acquire()'s real retry loop at the most retries any caller can configure, with
    # jitter pinned to its upper bound, and record every backoff it sleeps.
    slept: list[float] = []
    monkeypatch.setattr(random, "uniform", lambda a, b: b)
    monkeypatch.setattr(_lock, "_backoff_sleep", slept.append)
    monkeypatch.setattr(_lock, "check_store_compat", lambda _t: None)

    def never(*_a, **_k):
        raise _lock.LockTimeout(0)

    monkeypatch.setattr(_lock, "_acquire_once", never)
    with pytest.raises(_lock.LockTimeout):
        _lock.acquire("/nonexistent", timeout=1, attempts=1, retries=_lock._MAX_RETRIES)
    assert slept, "precondition: acquire slept between passes"

    paused: list[float] = []
    monkeypatch.setattr(_compact, "_pause", paused.append)
    _compact._give_waiters_a_turn(1.0)

    assert max(slept) <= _lock.max_waiter_gap_s(between_passes=True)
    assert paused[0] > max(slept)


# ── each fold is published before the next ───────────────────────────────────────────────────
@pytest.fixture
def store_with_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from rebar import config as _config

    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    init_bare_remote(origin)
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@t.co"),
        ("git", "config", "user.name", "t"),
        ("git", "remote", "add", "origin", str(origin)),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    for variable in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
        monkeypatch.delenv(variable, raising=False)
    _config.reset_config_cache()
    rebar.init_repo(repo_root=str(repo))
    yield repo
    _config.reset_config_cache()


def _unpushed(repo: Path) -> int:
    """Commits on the local tickets branch that the origin's tickets branch lacks."""
    tracker = repo / ".tickets-tracker"
    remote = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/tickets"],
        cwd=tracker,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD", *(f"^{sha}" for sha in remote[:1])],
        cwd=tracker,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _seed_foldable(repo: Path, count: int) -> None:
    for n in range(count):
        tid = rebar.create_ticket("task", f"t{n}", description="x" * 60, repo_root=str(repo))
        for i in range(3):
            rebar.comment(tid, f"c{i}", repo_root=str(repo))


def test_each_fold_reaches_the_origin_before_the_next_fold_starts(
    store_with_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = store_with_origin
    _seed_foldable(repo, 3)
    assert _unpushed(repo) == 0, "precondition: the seeding writes pushed"

    unpushed_between_folds: list[int] = []

    def probe(_tracker: str, done: int) -> bool:
        if done:
            unpushed_between_folds.append(_unpushed(repo))
        return False

    monkeypatch.setattr(_compact, "_sweep_should_yield", probe)
    monkeypatch.setattr(_compact, "_pause", lambda _s: None)

    assert _compact.compact_all_cli([], repo_root=str(repo)) == 0

    assert len(unpushed_between_folds) == 2, "precondition: the sweep folded all 3 tickets"
    assert unpushed_between_folds == [0, 0], (
        f"the clone held unpublished folds while the sweep went on: {unpushed_between_folds}"
    )
    assert _unpushed(repo) == 0


def test_a_no_commit_sweep_pushes_nothing(
    store_with_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._store import push

    repo = store_with_origin
    _seed_foldable(repo, 2)
    pushes: list[str] = []
    monkeypatch.setattr(push, "push_after_commit", lambda t: pushes.append(str(t)))
    monkeypatch.setattr(_compact, "_pause", lambda _s: None)

    assert _compact.compact_all_cli(["--no-commit"], repo_root=str(repo)) == 0

    assert pushes == []
