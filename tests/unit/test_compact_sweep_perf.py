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


# ── no history walk under the lock, even for an event the position map cannot see (4dd1) ─────
# The sweep builds one ``--no-merges`` position map up front and folds tickets for minutes. An
# event the map cannot attribute used to fall through to unbounded per-event ``git log`` walks
# INSIDE the store write lock (140 s each on a 300k-commit clone, against a 30 s timeout).
# Two real ways to miss the map: an event committed AFTER the map was built (the resolvers CAN
# find it), and an event only a merge commit adds (no resolver can: ``--diff-filter=A`` computes
# no diff for a merge, so that position is always unattributable and the walks only burn time).


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", _tracker(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _fabricated_signed_event(tdir: Path) -> tuple[str, str]:
    """Write one signed COMMENT event file (not yet committed); return ``(position, name)``."""
    import json
    import uuid

    template = sorted(tdir.glob("*-COMMENT.json"))[0]
    event = json.loads(template.read_text())
    event["uuid"] = str(uuid.uuid4())
    event["timestamp"] = int(event["timestamp"]) + 7
    event["author_sig"] = "synthetic-dsse-envelope"
    name = f"{event['timestamp']}-{event['uuid']}-COMMENT.json"
    (tdir / name).write_text(json.dumps(event))
    return f"{event['timestamp']}-{event['uuid']}", name


def _add_event_in_a_normal_commit(repo: Path, tid: str) -> tuple[str, str]:
    """A signed event committed normally. Returns ``(position, adding_commit_sha)``."""
    tdir = _tdir(repo, tid)
    position, name = _fabricated_signed_event(tdir)
    _git(repo, "add", str(tdir / name))
    _git(repo, "commit", "-q", "--no-verify", "-m", "a late signed event")
    return position, _git(repo, "rev-parse", "HEAD").strip()


def _add_event_only_a_merge_adds(repo: Path, tid: str) -> str:
    """A signed event no non-merge commit ever adds ("evil merge"); returns its position."""
    import uuid

    tdir = _tdir(repo, tid)
    _git(repo, "checkout", "-q", "-b", f"side-{uuid.uuid4().hex[:6]}")
    _git(repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", "side commit")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "-")
    position, name = _fabricated_signed_event(tdir)
    _git(repo, "add", str(tdir / name))
    _git(repo, "commit", "-q", "--no-verify", "-m", "evil merge introduces an event")
    assert name not in _git(
        repo, "log", "--diff-filter=A", "--no-merges", "--name-only", "--format=", "--", "*.json"
    ), "ANCHOR: the position map must not be able to see this event"
    return position


def _signed_ticket(repo: Path) -> str:
    tid = _seed(repo, "late events", comments=3)
    tdir = _tdir(repo, tid)
    _age(tdir)
    _sign_and_commit(repo, tdir)
    return tid


def _watch_walks_under_lock(monkeypatch: pytest.MonkeyPatch, repo: Path) -> list[str]:
    from rebar.attest import authorship

    lock_dir = Path(_tracker(repo)) / ".ticket-write.lock.d"
    seen: list[str] = []
    for name in (
        "build_position_commit_map",
        "build_ticket_position_commit_map",
        "resolve_event_commit",
        "resolve_position_commit",
    ):
        real = getattr(authorship, name)

        def wrapper(*a, _real=real, _name=name, **k):
            if lock_dir.exists():
                seen.append(_name)
            return _real(*a, **k)

        monkeypatch.setattr(authorship, name, wrapper)
    return seen


def _ledger_entry(repo: Path, tid: str, position: str) -> dict:
    import json

    snapshots = sorted(_tdir(repo, tid).glob("*-SNAPSHOT.json"))
    assert snapshots, "the sweep wrote no SNAPSHOT, so there is no ledger to inspect"
    ledger = json.loads(snapshots[-1].read_text())["data"]["compiled_state"]["authorship_ledger"]
    matches = [e for e in ledger if e["position"]["position"] == position]
    assert len(matches) == 1, f"expected one ledger entry for {position}, got {len(matches)}"
    return matches[0]


def _stale_sweep_map(monkeypatch: pytest.MonkeyPatch, repo: Path) -> dict[str, str]:
    """Freeze the sweep's map NOW, so events committed afterwards miss it, as in a long sweep."""
    from rebar.attest import authorship

    stale = authorship.build_position_commit_map(repo_root=str(repo))
    monkeypatch.setattr(_compact, "_sweep_position_map", lambda _repo_root: stale)
    return stale


def test_an_event_committed_after_the_map_was_built_triggers_no_walk_under_the_lock(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = store
    tid = _signed_ticket(repo)
    stale = _stale_sweep_map(monkeypatch, repo)
    position, adding_sha = _add_event_in_a_normal_commit(repo, tid)
    assert position not in stale, "ANCHOR: the frozen map must miss the late event"
    seen = _watch_walks_under_lock(monkeypatch, repo)

    _compact.compact_all_cli([], repo_root=str(repo))

    assert not seen, (
        "a git-history walk ran while the store write lock was held, so every concurrent "
        f"writer waits for it: {seen}"
    )
    assert _ledger_entry(repo, tid, position)["position"]["commit_sha"] == adding_sha, (
        "moving the walk out of the lock must still attribute the event to the commit that added it"
    )


def test_a_merge_only_event_triggers_no_walk_under_the_lock_and_stays_null(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = store
    tid = _signed_ticket(repo)
    position = _add_event_only_a_merge_adds(repo, tid)
    paths = sorted(str(p) for p in _tdir(repo, tid).glob("*.json") if not p.name.startswith("."))
    today = {
        e["position"]["position"]: e["position"]["commit_sha"]
        for e in _compact_txn._build_authorship_ledger(paths, str(repo))
    }
    assert today[position] is None, "ANCHOR: no resolver can attribute a merge-only event"
    seen = _watch_walks_under_lock(monkeypatch, repo)

    _compact.compact_all_cli([], repo_root=str(repo))

    assert not seen, f"a git-history walk ran while the store write lock was held: {seen}"
    assert _ledger_entry(repo, tid, position)["position"]["commit_sha"] is None


def test_an_unresolvable_position_persists_json_null_with_no_walk_under_the_lock(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.attest import authorship

    repo = store
    tid = _signed_ticket(repo)
    _stale_sweep_map(monkeypatch, repo)
    position, _sha = _add_event_in_a_normal_commit(repo, tid)
    monkeypatch.setattr(authorship, "resolve_event_commit", lambda *a, **k: None)
    monkeypatch.setattr(authorship, "resolve_position_commit", lambda *a, **k: None)
    seen = _watch_walks_under_lock(monkeypatch, repo)

    _compact.compact_all_cli([], repo_root=str(repo))

    assert not seen, f"an unresolvable position still walked history under the lock: {seen}"
    entry = _ledger_entry(repo, tid, position)
    assert entry["position"]["commit_sha"] is None, (
        "an unresolvable position must persist JSON null, never an empty string: "
        f"{entry['position']!r}"
    )


def test_a_resolver_timeout_persists_null_and_is_not_retried_under_the_lock(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeouts are the expected case on a large clone; they must cost the lock nothing."""
    from rebar.attest import authorship

    repo = store
    tid = _signed_ticket(repo)
    _stale_sweep_map(monkeypatch, repo)
    position, _sha = _add_event_in_a_normal_commit(repo, tid)
    lock_dir = Path(_tracker(repo)) / ".ticket-write.lock.d"
    real_run = authorship.subprocess.run
    resolver_runs_under_lock: list[int] = []

    def slow_resolvers(cmd, *a, **k):
        # The per-event resolvers are the git-log calls WITHOUT --no-merges.
        if "--diff-filter=A" in cmd and "--no-merges" not in cmd:
            if lock_dir.exists():
                resolver_runs_under_lock.append(1)
            raise subprocess.TimeoutExpired(cmd, 30)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(authorship.subprocess, "run", slow_resolvers)

    _compact.compact_all_cli([], repo_root=str(repo))

    assert not resolver_runs_under_lock, "a timed-out resolver was retried under the store lock"
    assert _ledger_entry(repo, tid, position)["position"]["commit_sha"] is None


def test_pre_resolving_outside_the_lock_leaves_the_ledger_unchanged(store: Path) -> None:
    from rebar.attest import authorship

    repo = store
    tid = _signed_ticket(repo)
    sweep_map = authorship.build_position_commit_map(repo_root=str(repo))
    late_position, adding_sha = _add_event_in_a_normal_commit(repo, tid)
    merge_position = _add_event_only_a_merge_adds(repo, tid)
    tdir = _tdir(repo, tid)
    paths = sorted(str(p) for p in tdir.glob("*.json") if not p.name.startswith("."))
    assert late_position not in sweep_map and merge_position not in sweep_map

    without = _compact_txn._build_authorship_ledger(paths, str(repo))
    resolved = _compact_txn.preresolve_unmapped_positions(
        str(tdir), _tracker(repo), str(repo), sweep_map
    )
    with_pre = _compact_txn._build_authorship_ledger(paths, str(repo), position_commits=resolved)

    assert late_position not in sweep_map, "the sweep map must not be mutated"
    assert resolved[late_position] == adding_sha
    assert resolved[merge_position] == "", "an unattributable position carries the marker"
    assert with_pre == without, "pre-resolution changed the authorship ledger"
    assert all(
        e["position"]["commit_sha"] is None or len(e["position"]["commit_sha"]) == 40
        for e in with_pre
    ), "the empty-string marker leaked into a persisted ledger entry"


def test_an_event_absent_from_the_map_without_a_marker_still_uses_the_fallback(
    store: Path, walk_counts: dict[str, int]
) -> None:
    """An event that arrives after pre-resolution keeps today's in-lock fallback."""
    repo = store
    tid = _signed_ticket(repo)
    position, adding_sha = _add_event_in_a_normal_commit(repo, tid)
    paths = sorted(str(p) for p in _tdir(repo, tid).glob("*.json") if not p.name.startswith("."))

    ledger = _compact_txn._build_authorship_ledger(paths, str(repo), position_commits={"x": "y"})

    entry = next(e for e in ledger if e["position"]["position"] == position)
    assert entry["position"]["commit_sha"] == adding_sha
    assert walk_counts["per_event"] + walk_counts["per_position"] >= 1, (
        "an absent position must keep today's fallback walk"
    )


def test_a_map_free_ledger_never_carries_the_empty_string_marker(store: Path) -> None:
    """The fsck rebuild path passes no map; its entries must be real SHAs or null."""
    repo = store
    tid = _signed_ticket(repo)
    _add_event_only_a_merge_adds(repo, tid)
    paths = sorted(str(p) for p in _tdir(repo, tid).glob("*.json") if not p.name.startswith("."))

    for entry in _compact_txn._build_authorship_ledger(paths, str(repo)):
        sha = entry["position"]["commit_sha"]
        assert sha is None or len(sha) == 40, entry
