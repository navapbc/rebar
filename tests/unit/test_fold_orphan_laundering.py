"""The natural fold must not LAUNDER orphans (bug winning-endurant-xenops,
f96b-3498-8f04-40b0).

THE DEFECT. The fold-horizon race (ADR-0035's RC2 class: a concurrent write union-merges
in AFTER a fold enumerated its inputs) leaves an active pre-snapshot event absent from the
snapshot's ``source_event_uuids``. fsck flags it ORPHAN_EVENT and
``fsck --repair-snapshots`` heals it from the full log — but only while the evidence
survives. The NEXT natural fold destroyed it: the whole-dir enumeration RETIRED the
orphan's file and CITED its uuid in the new snapshot while the replay's positional skip
kept its effect OUT of the compiled state. fsck then reported clean; repairable damage
became silent, undetectable loss at the first threshold crossing.

THE FIX (pinned here):
* The fold EXCLUDES orphans from both retirement and citation, using exactly fsck's
  orphan predicate (shared helper ``fsck_repair.is_snapshot_orphan``) anchored to a prior
  snapshot present in the fold set. The orphan stays a live, un-cited file, so the
  ORPHAN_EVENT finding and the repair window survive indefinitely. The fold does NOT
  absorb (that would bypass the auto-recover/human-triage routing).
* First fold (no prior snapshot in the dir): nothing is excluded — the new snapshot's own
  citation list plays no role in the predicate.
* Routing parity: ``fsck --repair-snapshots`` no longer rebuilds past
  ``_HUMAN_TRIAGE_ORPHAN_TYPES`` — an order-sensitive orphan blocks the whole-ticket
  rebuild (a rebuild would silently absorb it in log order), exactly as ``fsck --repair``
  already refuses.
* Defer guard: pre-snapshot foldables never fold WITHOUT their governing snapshot (that
  fold would write a pre-snapshot SNAPSHOT the governing one positionally buries).
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid as _uuid
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._commands import fsck as _fsck
from rebar._commands.fsck_scan import _check_snapshot
from rebar._store.event_prepare import event_filename
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.unit


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
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _tdir(repo: Path, tid: str) -> Path:
    return _tracker(repo) / tid


def _comments(repo: Path, tid: str) -> list[str]:
    state = reduce_ticket(str(_tdir(repo, tid)))
    assert state is not None
    return [c.get("body", "") for c in state.get("comments", [])]


def _snapshots(repo: Path, tid: str) -> list[Path]:
    return sorted(_tdir(repo, tid).glob("*-SNAPSHOT.json"))


def _latest_sources(repo: Path, tid: str) -> set[str]:
    snap = _snapshots(repo, tid)[-1]
    return set(json.loads(snap.read_text(encoding="utf-8"))["data"]["source_event_uuids"])


def _orphan_findings(repo: Path, tid: str) -> list[str]:
    tdir = str(_tdir(repo, tid))
    out: list[str] = []
    for snap in sorted(
        n for n in os.listdir(tdir) if n.endswith("-SNAPSHOT.json") and not n.startswith(".")
    ):
        out.extend(f for f in _check_snapshot(tdir, tid, snap) if f.startswith("ORPHAN_EVENT"))
    return out


def _fold(repo: Path, tid: str, *extra: str) -> int:
    return _compact.compact_cli(
        [tid, "--threshold=0", "--horizon=0", "--skip-sync", *extra], repo_root=str(repo)
    )


def _seed(repo: Path, title: str, comments: int = 3) -> str:
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return tid


def _commit_all(repo: Path, msg: str) -> None:
    for args in (("git", "add", "-A"), ("git", "commit", "-q", "-m", msg)):
        subprocess.run(args, cwd=_tracker(repo), check=True, capture_output=True)


def _race_orphan(repo: Path, tid: str, body: str = "late arrival") -> Path:
    """Reproduce the fold-horizon race deterministically.

    Append the comment normally (a real, signed event file), HIDE its file (the remote
    clone's commit not yet merged), fold the rest into a SNAPSHOT, then land the file
    back COMMITTED, exactly as the union merge does: pre-snapshot timestamp, uuid absent
    from ``source_event_uuids``."""
    rebar.comment(tid, body, repo_root=str(repo))
    tdir = _tdir(repo, tid)
    candidates = [
        p
        for p in tdir.glob("*.json")
        if json.loads(p.read_text(encoding="utf-8")).get("data", {}).get("body") == body
    ]
    assert len(candidates) == 1
    orphan = candidates[0]
    hidden = tdir / f".hidden-{orphan.name}"
    orphan.rename(hidden)
    assert _fold(repo, tid) == 0
    hidden.rename(orphan)
    _commit_all(repo, "simulate union merge landing the late comment")
    snap = _snapshots(repo, tid)[-1]
    assert orphan.name < snap.name, "precondition: orphan sorts before the snapshot"
    orphan_uuid = json.loads(orphan.read_text(encoding="utf-8"))["uuid"]
    assert orphan_uuid not in _latest_sources(repo, tid), (
        "precondition: orphan not captured by the snapshot"
    )
    assert _orphan_findings(repo, tid), "precondition: fsck flags ORPHAN_EVENT"
    return orphan


def _plant_orphan_file(repo: Path, tid: str, event_type: str, data: dict) -> Path:
    """Land a crafted pre-snapshot, un-cited event file as the union merge would."""
    tdir = _tdir(repo, tid)
    snap = _snapshots(repo, tid)[-1]
    snap_ts = int(snap.name.split("-")[0])
    u = str(_uuid.uuid4())
    ts = snap_ts - 1000
    path = tdir / event_filename(ts, u, event_type)
    path.write_text(
        json.dumps({"event_type": event_type, "timestamp": ts, "uuid": u, "data": data}),
        encoding="utf-8",
    )
    _commit_all(repo, f"simulate union merge landing a {event_type} orphan")
    assert path.name < snap.name and u not in _latest_sources(repo, tid)
    return path


# ── G1: the laundering, and its fix ─────────────────────────────────────────────────────────
def test_threshold_crossing_fold_does_not_launder_orphan(store: Path) -> None:
    """THE BUG (RED on pre-fix main): the next natural fold retired the orphan's file and
    cited its uuid while the positional skip dropped its effect — fsck went clean on
    permanently lost data. The fold must EXCLUDE the orphan from retirement + citation."""
    repo = store
    tid = _seed(repo, "laundering victim")
    orphan = _race_orphan(repo, tid)
    orphan_uuid = json.loads(orphan.read_text(encoding="utf-8"))["uuid"]
    for i in range(2):
        rebar.comment(tid, f"p{i}", repo_root=str(repo))

    # Live files now: snapshot + orphan + p0 + p1 — a genuine threshold crossing.
    assert _fold(repo, tid, "--threshold=2") == 0

    assert orphan.exists(), "the orphan must stay a live file (not retired)"
    assert not (orphan.parent / (orphan.name + ".retired")).exists()
    assert orphan_uuid not in _latest_sources(repo, tid), (
        "the new snapshot must NOT cite the orphan it did not apply"
    )
    assert _orphan_findings(repo, tid), (
        "fsck must still flag ORPHAN_EVENT after the fold — the repair window survives"
    )
    bodies = _comments(repo, tid)
    assert "late arrival" not in bodies, "the fold must NOT absorb (repair owns that)"
    assert bodies == ["c0", "c1", "c2", "p0", "p1"], f"folded state must be intact; got {bodies}"


def test_repair_window_survives_fold_then_repair_restores(store: Path) -> None:
    """Integration AC: orphan survives a threshold-crossing fold, then
    ``fsck --repair-snapshots`` (the existing ADR-0035 door) restores the comment."""
    repo = store
    tid = _seed(repo, "repaired after fold")
    _race_orphan(repo, tid)
    rebar.comment(tid, "post", repo_root=str(repo))
    assert _fold(repo, tid, "--threshold=1") == 0

    rc = _fsck.fsck_cli(["--repair-snapshots"], repo_root=str(repo))

    assert "late arrival" in _comments(repo, tid), (
        f"repair-snapshots must restore the orphan comment (fsck rc={rc})"
    )
    assert not _orphan_findings(repo, tid), "the rebuilt snapshot must cite the orphan"


def test_first_fold_excludes_nothing(store: Path) -> None:
    """A first fold (no prior SNAPSHOT in the dir) folds every event normally — the new
    snapshot's own citation list plays no role in the exclusion predicate."""
    repo = store
    tid = _seed(repo, "first fold", comments=2)
    tdir = _tdir(repo, tid)
    live_before = sorted(p.name for p in tdir.glob("*.json") if not p.name.endswith("-SYNC.json"))

    assert _fold(repo, tid) == 0

    for name in live_before:
        assert not (tdir / name).exists(), f"{name} must fold on a first fold"
        assert (tdir / (name + ".retired")).exists()
    assert _comments(repo, tid) == ["c0", "c1"]
    assert not _orphan_findings(repo, tid)


def test_healthy_refold_excludes_nothing(store: Path) -> None:
    """A re-fold of a healthy ticket (prior snapshot cites all pre-snapshot events)
    excludes nothing: the old snapshot and the post-snapshot events all fold."""
    repo = store
    tid = _seed(repo, "healthy refold", comments=2)
    assert _fold(repo, tid) == 0
    old_snap = _snapshots(repo, tid)[-1]
    for i in range(2):
        rebar.comment(tid, f"p{i}", repo_root=str(repo))
    post_uuids = {
        json.loads(p.read_text(encoding="utf-8"))["uuid"]
        for p in _tdir(repo, tid).glob("*.json")
        if not p.name.endswith("-SNAPSHOT.json") and not p.name.endswith("-SYNC.json")
    }

    assert _fold(repo, tid) == 0

    assert not old_snap.exists(), "the prior snapshot folds on a healthy re-fold"
    assert post_uuids <= _latest_sources(repo, tid), "post-snapshot events fold and are cited"
    assert _comments(repo, tid) == ["c0", "c1", "p0", "p1"]
    assert not _orphan_findings(repo, tid)


def test_presnapshot_foldables_defer_when_snapshot_is_young(store: Path) -> None:
    """Defer guard: an orphan outside the horizon must NOT fold while its governing
    snapshot is inside it — that fold would write a pre-snapshot SNAPSHOT the governing
    one positionally buries (silent, permanent loss)."""
    repo = store
    tid = _seed(repo, "deferred")
    orphan = _race_orphan(repo, tid)
    snaps_before = _snapshots(repo, tid)

    # Re-time the orphan far into the past so IT alone is foldable under a wide horizon.
    tdir = _tdir(repo, tid)
    ev = json.loads(orphan.read_text(encoding="utf-8"))
    ev["timestamp"] = 1_000_000
    old_name = event_filename(1_000_000, ev["uuid"], "COMMENT")
    (tdir / old_name).write_text(json.dumps(ev), encoding="utf-8")
    orphan.unlink()

    rc = _compact.compact_cli(
        [tid, "--threshold=0", f"--horizon={10**18}", "--skip-sync"], repo_root=str(repo)
    )
    assert rc == 0, "deferring is a clean skip, not a failure"
    assert _snapshots(repo, tid) == snaps_before, (
        "no new snapshot may be written from pre-snapshot candidates alone"
    )
    assert (tdir / old_name).exists(), "the deferred orphan must stay live (not retired)"


def test_cited_presnapshot_event_is_not_an_orphan(store: Path) -> None:
    """The uuid check is what separates an orphan from a merely-unretired folded source
    (SNAPSHOT_INCONSISTENT): a pre-snapshot active event that IS cited by the snapshot
    was applied — the fold must fold/retire it normally, never exclude it, and fsck
    must not call it ORPHAN_EVENT."""
    repo = store
    tid = _seed(repo, "cited is not orphan", comments=2)
    assert _fold(repo, tid) == 0
    tdir = _tdir(repo, tid)
    retired = sorted(tdir.glob("*-COMMENT.json.retired"))[0]
    live = tdir / retired.name.removesuffix(".retired")
    retired.rename(live)  # un-retire a folded source: cited + active + pre-snapshot
    _commit_all(repo, "simulate a crash that left a folded source unretired")
    assert not _orphan_findings(repo, tid), "a cited event must never be ORPHAN_EVENT"

    assert _fold(repo, tid) == 0

    assert not live.exists(), "a cited pre-snapshot event folds normally (not excluded)"
    assert (tdir / (live.name + ".retired")).exists()
    assert _comments(repo, tid) == ["c0", "c1"]
    assert not _orphan_findings(repo, tid)


# ── G4: routing parity for --repair-snapshots ───────────────────────────────────────────────
def test_repair_snapshots_skips_human_triage_orphan(store: Path) -> None:
    """An order-sensitive orphan (STATUS is in _HUMAN_TRIAGE_ORPHAN_TYPES) must NOT
    trigger the whole-ticket rebuild — parity with fsck --repair's routing."""
    repo = store
    tid = _seed(repo, "triage stays human", comments=2)
    assert _fold(repo, tid) == 0
    planted = _plant_orphan_file(
        repo, tid, "STATUS", {"current_status": "open", "target_status": "in_progress"}
    )
    snaps_before = _snapshots(repo, tid)

    rc = _fsck.fsck_cli(["--repair-snapshots"], repo_root=str(repo))

    assert _snapshots(repo, tid) == snaps_before, (
        "a HUMAN_TRIAGE orphan must not be absorbed by a rebuild"
    )
    assert planted.exists(), "the triage orphan stays live for human review"
    assert _orphan_findings(repo, tid), "the finding must persist (damage not repaired)"
    assert rc == 1, "unrepaired damage keeps failing fsck"


def test_repair_snapshots_blocks_rebuild_when_triage_orphan_coexists(store: Path) -> None:
    """A ticket holding BOTH an auto-recover orphan and a triage orphan is not rebuilt:
    the rebuild is whole-ticket, so it would absorb the order-sensitive one."""
    repo = store
    tid = _seed(repo, "mixed orphans", comments=2)
    _race_orphan(repo, tid)  # COMMENT: _AUTO_RECOVER
    _plant_orphan_file(
        repo, tid, "STATUS", {"current_status": "open", "target_status": "in_progress"}
    )
    snaps_before = _snapshots(repo, tid)

    _fsck.fsck_cli(["--repair-snapshots"], repo_root=str(repo))

    assert _snapshots(repo, tid) == snaps_before, (
        "the triage orphan's presence must block the whole-ticket rebuild"
    )
    assert "late arrival" not in _comments(repo, tid)


def test_repair_snapshots_still_rebuilds_auto_recover_orphan(store: Path) -> None:
    """The routing must not over-block: a commutative (auto-recover) orphan alone still
    rebuilds, exactly as before."""
    repo = store
    tid = _seed(repo, "auto still heals")
    _race_orphan(repo, tid)

    _fsck.fsck_cli(["--repair-snapshots"], repo_root=str(repo))

    assert "late arrival" in _comments(repo, tid)
    assert not _orphan_findings(repo, tid)
