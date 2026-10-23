"""A store-wide sweep builds one position-to-commit map before taking the
write lock and shares it across ticket folds. Tests count both map builds
and fallback history walks so a map keyed by the wrong identity cannot
silently restore per-event git log work.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._commands import compact_txn as _compact_txn

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
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> str:
    return str(rebar.config.tracker_dir(str(repo)))


def _tdir(repo: Path, tid: str) -> Path:
    return Path(_tracker(repo)) / tid


def _seed(repo: Path, title: str, comments: int) -> str:
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return rebar._engine_support.resolver.resolve_ticket_id(tid, _tracker(repo))


def _age(tdir: Path, by_ns: int = _HOUR_NS) -> None:
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


def _sign_and_commit(repo: Path, tdir: Path) -> None:
    """Commit each signed fixture event before building the authorship ledger.
    Unsigned events are skipped, and uncommitted events are absent from the git
    position map. A synthetic signature still produces a ledger entry with an
    unresolved signer key.
    """
    for path in sorted(tdir.glob("*.json")):
        if path.name.startswith("."):
            continue
        event = json.loads(path.read_text())
        event["author_sig"] = "synthetic-dsse-envelope"
        event.setdefault("author_id", "594c-9dcf-5ad6-4e6d")
        path.write_text(json.dumps(event))
    tracker = _tracker(repo)
    subprocess.run(["git", "-C", tracker, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", "test: sign fixture events"],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def walk_counts(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every history-walking entry point the ledger can reach.

    Counting only the per-sweep builder would let a key-shape mismatch pass: the map would be
    built once, miss every lookup, and each event would quietly walk history on its own.
    """
    from rebar.attest import authorship

    counts = {"sweep_map": 0, "ticket_map": 0, "per_event": 0, "per_position": 0}
    for name, key in (
        ("build_position_commit_map", "sweep_map"),
        ("build_ticket_position_commit_map", "ticket_map"),
        ("resolve_event_commit", "per_event"),
        ("resolve_position_commit", "per_position"),
    ):
        real = getattr(authorship, name, None)
        if real is None:
            continue

        def wrapper(*a, _real=real, _key=key, **k):
            counts[_key] += 1
            return _real(*a, **k)

        monkeypatch.setattr(authorship, name, wrapper)
    return counts


# ── one walk per sweep, and no silent per-event fallback ─────────────────────────────────────
def test_a_sweep_walks_history_once_regardless_of_ticket_count(
    store: Path, walk_counts: dict[str, int]
) -> None:
    """RED on the pre-fix code: one `build_ticket_position_commit_map` per ticket."""
    repo = store
    for n in range(4):
        tdir = _tdir(repo, _seed(repo, f"t{n}", comments=3))
        _age(tdir)
        _sign_and_commit(repo, tdir)

    _compact.compact_all_cli([], repo_root=str(repo))

    assert walk_counts["sweep_map"] == 1, (
        f"the sweep must build exactly one position map, got {walk_counts}"
    )
    assert walk_counts["ticket_map"] == 0, (
        "a per-ticket history walk ran during the sweep — the prebuilt map was not used, so "
        f"the cost still scales with ticket count: {walk_counts}"
    )
    assert walk_counts["per_event"] == 0 and walk_counts["per_position"] == 0, (
        "events fell back to per-event history resolution, which is what the prebuilt map "
        f"exists to prevent (a key-shape mismatch looks exactly like this): {walk_counts}"
    )


def test_the_single_ticket_path_still_builds_its_own_map(
    store: Path, walk_counts: dict[str, int]
) -> None:
    """`rebar store compact <id>` folds ONE ticket, where a per-ticket walk is already the right
    cost. The optimisation must not change it — this is the control proving the sweep result
    above comes from the prebuilt map, not from the walk having been removed outright."""
    repo = store
    tid = _seed(repo, "single", comments=3)
    _age(_tdir(repo, tid))

    _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo))

    assert walk_counts["ticket_map"] == 1, (
        f"the single-ticket fold should still build its own map: {walk_counts}"
    )
    assert walk_counts["sweep_map"] == 0


# ── no history walk inside the store write lock ──────────────────────────────────────────────
def test_no_history_walk_runs_while_the_store_lock_is_held(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural claim: `git log` is read-only and must never sit in the critical section.

    Observes STATE, not duration — at each walk, is the store's lock dir present?
    """
    from rebar.attest import authorship

    repo = store
    lock_dir = Path(_tracker(repo)) / ".ticket-write.lock.d"
    walks_under_lock: list[str] = []

    for name in ("build_position_commit_map", "build_ticket_position_commit_map"):
        real = getattr(authorship, name)

        def wrapper(*a, _real=real, _name=name, **k):
            if lock_dir.exists():
                walks_under_lock.append(_name)
            return _real(*a, **k)

        monkeypatch.setattr(authorship, name, wrapper)

    for n in range(3):
        _age(_tdir(repo, _seed(repo, f"t{n}", comments=3)))

    _compact.compact_all_cli([], repo_root=str(repo))

    assert not walks_under_lock, (
        "a git-history walk ran while the store write lock was held — every concurrent writer "
        f"waits for it: {walks_under_lock}"
    )


# ── the ledger is unchanged by the optimisation ──────────────────────────────────────────────
def test_ledger_is_identical_with_and_without_a_prebuilt_map(store: Path) -> None:
    """Correctness floor: the prebuilt map may only make the ledger FASTER, never different."""
    from rebar.attest import authorship

    repo = store
    tid = _seed(repo, "ledger parity", comments=3)
    tdir = _tdir(repo, tid)
    _age(tdir)
    _sign_and_commit(repo, tdir)
    paths = sorted(str(p) for p in tdir.glob("*.json") if not p.name.startswith("."))

    without = _compact_txn._build_authorship_ledger(paths, str(repo))
    prebuilt = authorship.build_position_commit_map(repo_root=str(repo))
    with_map = _compact_txn._build_authorship_ledger(paths, str(repo), position_commits=prebuilt)

    assert without, "the fixture produced an EMPTY ledger, so this compares nothing"
    assert with_map == without, "the prebuilt map changed the authorship ledger"


def test_the_map_argument_is_optional_for_the_rebuild_path(store: Path) -> None:
    """compact_rebuild omits the optional position map. The ledger must still
    contain the expected commit SHAs, not merely return a list.
    """
    from rebar.attest import authorship

    repo = store
    tid = _seed(repo, "rebuild caller", comments=2)
    tdir = _tdir(repo, tid)
    _age(tdir)
    _sign_and_commit(repo, tdir)
    paths = sorted(str(p) for p in tdir.glob("*.json") if not p.name.startswith("."))

    omitted = _compact_txn._build_authorship_ledger(paths, str(repo))
    explicit = _compact_txn._build_authorship_ledger(
        paths, str(repo), position_commits=authorship.build_position_commit_map(repo_root=str(repo))
    )

    assert omitted == explicit, (
        "omitting the map produced a DIFFERENT ledger from supplying it — the None-default "
        "branch is not equivalent to the prebuilt one"
    )
    assert omitted, "the fixture must produce a non-empty ledger, or this asserts nothing"
    for entry in omitted:
        assert entry.get("event_uuid"), f"ledger entry missing event_uuid: {entry}"
        assert entry.get("content_hash"), f"ledger entry missing content_hash: {entry}"
        assert entry.get("position", {}).get("commit_sha"), (
            f"ledger entry has no introducing commit — the walk resolved nothing: {entry}"
        )


# ── the compaction commit contains only what the sweep folded ────────────────────────────────
def test_the_sweep_commit_does_not_steal_a_concurrent_writers_event(store: Path) -> None:
    """The sweep stages only its fold paths. A concurrent writer's event in
    a settled bystander ticket must remain outside the compaction commit.
    """
    repo = store
    tid = _seed(repo, "swept", comments=3)
    _age(_tdir(repo, tid))

    other = _seed(repo, "bystander", comments=1)
    # Fold it now, so it carries a SNAPSHOT and sits below the threshold for the real sweep.
    _compact.compact_cli([other, "--threshold=0", "--skip-sync"], repo_root=str(repo))
    assert list(_tdir(repo, other).glob("*-SNAPSHOT.json")), "precondition: bystander is settled"

    # An uncommitted event file from another session, mid-flight in the worktree.
    stray = (
        _tdir(repo, other) / "1999999999999999999-aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa-COMMENT.json"
    )
    stray.write_text(
        json.dumps(
            {
                "timestamp": 1999999999999999999,
                "uuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
                "event_type": "COMMENT",
                "data": {"body": "mid-flight"},
            }
        )
    )

    _compact.compact_all_cli([], repo_root=str(repo))

    tracker = _tracker(repo)
    committed = subprocess.run(
        ["git", "-C", tracker, "show", "--name-only", "--format=", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert stray.name not in committed, (
        "the sweep committed a concurrent writer's in-flight event under its own message:\n"
        + committed
    )
    assert stray.exists(), "the bystander's file must be left alone, not consumed"
    # Positive half: scoped staging must still stage what the sweep DID fold. Without this the
    # test passes when `git add -- {tid}/` stages nothing at all, which would "fix" the stray
    # by committing nothing — the assertion above cannot tell those apart.
    folded_snapshots = [p.name for p in _tdir(repo, tid).glob("*-SNAPSHOT.json")]
    assert folded_snapshots, "precondition: the swept ticket was folded"
    assert any(name in committed for name in folded_snapshots), (
        "the scoped staging committed NOTHING — the folded SNAPSHOT is absent from HEAD:\n"
        + committed
    )


# ── the sweep re-yields to writers that arrive mid-run ───────────────────────────────────────
def test_the_sweep_yields_when_a_writer_takes_the_lock_mid_run(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trigger probes the lock once before starting, which only helps a sweep that was
    never going to hurt. A run that begins on a quiet store and folds for minutes must look
    again, or every writer arriving mid-run waits it out."""
    repo = store
    for n in range(4):
        _age(_tdir(repo, _seed(repo, f"t{n}", comments=3)))

    # Busy from the second probe onward: the sweep folds at most one ticket, then stands aside.
    calls = {"n": 0}

    def busy_after_first(_tracker):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(_compact._lock, "write_lock_is_busy", busy_after_first)

    _compact.compact_all_cli([], repo_root=str(repo))

    folded = [t for t in os.listdir(_tracker(repo)) if not t.startswith(".")]
    snapshotted = [t for t in folded if list((Path(_tracker(repo)) / t).glob("*-SNAPSHOT.json"))]
    assert len(snapshotted) <= 1, (
        f"the sweep kept folding after a writer took the lock (folded {len(snapshotted)})"
    )


# ── an unbuildable map must degrade to per-TICKET walks, never per-EVENT ─────────────────────
def test_a_failed_map_build_degrades_to_per_ticket_not_per_event(
    store: Path, monkeypatch: pytest.MonkeyPatch, walk_counts: dict[str, int]
) -> None:
    """A failed sweep map build passes None so each ticket builds one fallback
    map. Passing an empty dict would miss every position and trigger per-event
    history walks under the write lock.
    """
    from rebar.attest import authorship

    repo = store
    for n in range(2):
        tdir = _tdir(repo, _seed(repo, f"t{n}", comments=3))
        _age(tdir)
        _sign_and_commit(repo, tdir)

    def boom(**_kwargs):
        raise RuntimeError("git unavailable")

    monkeypatch.setattr(authorship, "build_position_commit_map", boom)

    _compact.compact_all_cli([], repo_root=str(repo))

    assert walk_counts["ticket_map"] >= 1, (
        "a failed map build must fall back to the per-TICKET walk, which is the pre-change "
        f"behaviour: {walk_counts}"
    )
    assert walk_counts["per_event"] == 0 and walk_counts["per_position"] == 0, (
        "a failed map build sent events down the PER-EVENT resolver — strictly worse than the "
        f"per-ticket walk it replaced: {walk_counts}"
    )
