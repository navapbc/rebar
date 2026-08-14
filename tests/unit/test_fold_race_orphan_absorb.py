"""Distributed fold-horizon race: orphan absorption (bug winning-endurant-xenops,
f96b-3498-8f04-40b0).

THE DEFECT. A remote writer commits a COMMENT on its own clone; a local session runs
COMPACT on the same ticket BEFORE the remote push merges. The union merge then lands the
comment file with a timestamp BEFORE the new snapshot and a uuid ABSENT from its
``source_event_uuids``. Nothing is lost on disk, but the reducer's replay (snapshot +
post-snapshot events) positionally skips the file: the comment is INVISIBLE in ``show``
while fsck flags ORPHAN_EVENT — and plain ``rebar compact`` refuses the re-fold ("remote
SNAPSHOT exists"), so there was no repair door.

THE FIX (pinned here):
* Absorbing re-fold — a fold whose input set includes the live snapshot treats an
  unretired pre-snapshot event missing from ``source_event_uuids`` as a LATE ARRIVAL and
  folds it into the next snapshot. Ordering semantics: snapshot first, then orphans by
  timestamp, then post-snapshot events.
* Safety guard — pre-snapshot candidates NEVER fold without their governing snapshot
  (folding them alone would write a pre-snapshot SNAPSHOT the governing one then
  positionally buries).
* Repair door — ``rebar doctor --repair`` routes ORPHAN_EVENT findings to that absorbing
  re-fold (``compact --absorb-orphans`` bypasses only the remote-SNAPSHOT refusal), with a
  ``refs/rebar-doctor/<utc-ts>`` backup ref before mutation.
* Never re-absorb — a retired file, or a live file already cited in
  ``source_event_uuids``, is re-cited at most, never re-applied.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._commands import doctor as _doctor
from rebar._commands.fsck_scan import _check_snapshot
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


def _race_orphan(repo: Path, tid: str, body: str = "late arrival") -> Path:
    """Reproduce the fold-horizon race deterministically.

    Append the comment normally (a real, signed event file), HIDE its file (the remote
    clone's commit not yet merged), fold the rest into a SNAPSHOT, then land the file back
    exactly as the union merge does: pre-snapshot timestamp, uuid absent from
    ``source_event_uuids``."""
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
    # The union merge lands the late file COMMITTED; a scratch-dir rename leaves it
    # untracked/dirty, which is a different damage class (doctor's dirty scan).
    for args in (
        ("git", "add", "-A"),
        ("git", "commit", "-q", "-m", "simulate union merge landing the late comment"),
    ):
        subprocess.run(args, cwd=_tracker(repo), check=True, capture_output=True)
    snap = _snapshots(repo, tid)[-1]
    assert orphan.name < snap.name, "precondition: orphan sorts before the snapshot"
    cited = json.loads(snap.read_text(encoding="utf-8"))["data"]["source_event_uuids"]
    orphan_uuid = json.loads(orphan.read_text(encoding="utf-8"))["uuid"]
    assert orphan_uuid not in cited, "precondition: orphan not captured by the snapshot"
    return orphan


def _backup_refs(repo: Path) -> list[str]:
    cp = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/rebar-doctor/"],
        cwd=_tracker(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in cp.stdout.splitlines() if line]


# ── the race, reproduced: invisible in show, flagged by fsck ────────────────────────────────
def test_race_reproduction_orphan_is_invisible_and_flagged(store: Path) -> None:
    repo = store
    tid = _seed(repo, "race victim")
    _race_orphan(repo, tid)

    assert "late arrival" not in _comments(repo, tid), (
        "oracle precondition: the positional skip hides the orphan comment"
    )
    assert _orphan_findings(repo, tid), "oracle precondition: fsck flags ORPHAN_EVENT"


# ── absorbing re-fold: the fold folds the late arrival into the next snapshot ───────────────
def test_absorb_refold_restores_visibility_and_clears_fsck(store: Path) -> None:
    repo = store
    tid = _seed(repo, "absorbed")
    orphan = _race_orphan(repo, tid)

    assert _fold(repo, tid) == 0, "the re-fold (skip-sync path) must succeed"

    assert "late arrival" in _comments(repo, tid), "the absorbed orphan must be visible"
    assert not _orphan_findings(repo, tid), "the re-fold must clear ORPHAN_EVENT"
    assert not orphan.exists(), "the absorbed orphan must be retired"
    snap = _snapshots(repo, tid)[-1]
    cited = json.loads(snap.read_text(encoding="utf-8"))["data"]["source_event_uuids"]
    orphan_uuid = json.loads((orphan.parent / (orphan.name + ".retired")).read_text())["uuid"]
    assert orphan_uuid in cited, "the new snapshot must cite the absorbed orphan"


def test_absorb_ordering_snapshot_then_orphans_then_post(store: Path) -> None:
    """Pin ONE ordering: snapshot's absorbed state first, then orphans by ts, then
    post-snapshot events — an orphan timestamped between folded comments does NOT
    interleave back among them."""
    repo = store
    tid = _seed(repo, "ordering", comments=2)
    orphan = _race_orphan(repo, tid, body="middle")
    # Retime the orphan BETWEEN c0 and c1 (filename + payload), keeping it pre-snapshot.
    tdir = _tdir(repo, tid)
    comment_files = sorted(
        p
        for p in tdir.glob("*.json.retired")
        if json.loads(p.read_text()).get("event_type") == "COMMENT"
    )
    first_ts = int(comment_files[0].name.split("-")[0])
    ev = json.loads(orphan.read_text(encoding="utf-8"))
    ev["timestamp"] = first_ts + 1
    new_name = f"{first_ts + 1}-{ev['uuid']}-COMMENT.json"
    (tdir / new_name).write_text(json.dumps(ev), encoding="utf-8")
    orphan.unlink()

    rebar.comment(tid, "after", repo_root=str(repo))
    assert _fold(repo, tid) == 0

    bodies = _comments(repo, tid)
    assert bodies == ["c0", "c1", "middle", "after"], (
        f"pinned order: snapshot state, then orphans by ts, then post-snapshot events; got {bodies}"
    )


def test_presnapshot_candidates_defer_when_snapshot_is_young(store: Path) -> None:
    """The safety guard: an orphan outside the horizon must NOT fold while its governing
    snapshot is still inside it — that fold would write a pre-snapshot SNAPSHOT the
    governing one positionally buries (silent, permanent loss)."""
    repo = store
    tid = _seed(repo, "deferred")
    orphan = _race_orphan(repo, tid)
    snaps_before = _snapshots(repo, tid)

    # Horizon wide enough that the snapshot (and everything else) is young; the orphan's
    # file is re-timed far into the past so IT alone is foldable.
    tdir = _tdir(repo, tid)
    ev = json.loads(orphan.read_text(encoding="utf-8"))
    ev["timestamp"] = 1_000_000
    old_name = f"1000000-{ev['uuid']}-COMMENT.json"
    (tdir / old_name).write_text(json.dumps(ev), encoding="utf-8")
    orphan.unlink()

    rc = _compact.compact_cli(
        [tid, "--threshold=0", f"--horizon={10**18}", "--skip-sync"], repo_root=str(repo)
    )
    assert rc == 0
    assert _snapshots(repo, tid) == snaps_before, (
        "no new snapshot may be written from pre-snapshot candidates alone"
    )
    assert (tdir / old_name).exists(), "the deferred orphan must stay live (not retired)"


# ── the repair door ──────────────────────────────────────────────────────────────────────────
def test_plain_compact_refusal_stays(store: Path) -> None:
    repo = store
    tid = _seed(repo, "still refused")
    _race_orphan(repo, tid)
    snaps_before = _snapshots(repo, tid)

    rc = _compact.compact_cli([tid, "--threshold=0", "--horizon=0"], repo_root=str(repo))
    assert rc == 0
    assert _snapshots(repo, tid) == snaps_before, (
        "plain compact must still refuse when a SNAPSHOT exists"
    )
    assert "late arrival" not in _comments(repo, tid)


def test_absorb_flag_bypasses_refusal(store: Path) -> None:
    repo = store
    tid = _seed(repo, "flagged through")
    _race_orphan(repo, tid)

    rc = _compact.compact_cli(
        [tid, "--threshold=0", "--horizon=0", "--absorb-orphans"], repo_root=str(repo)
    )
    assert rc == 0
    assert "late arrival" in _comments(repo, tid)
    assert not _orphan_findings(repo, tid)


def test_doctor_scan_reports_orphan_readonly(store: Path) -> None:
    repo = store
    tid = _seed(repo, "doctor sees it")
    orphan = _race_orphan(repo, tid)

    findings = _doctor.scan_orphans(str(_tracker(repo)))
    assert any(
        f["kind"] == "ORPHAN_EVENT" and f"{tid}/{orphan.name}" in f["paths"] for f in findings
    ), f"doctor scan must surface the orphan finding; got {findings}"
    assert "late arrival" not in _comments(repo, tid), "scan must be read-only"


def test_doctor_repair_absorbs_with_backup_ref(store: Path) -> None:
    repo = store
    tid = _seed(repo, "doctor heals")
    _race_orphan(repo, tid)
    assert not _backup_refs(repo)

    rc = _doctor.doctor_cli(["--repair"], repo_root=str(repo))
    assert rc == 0, "doctor --repair must exit 0 once the orphan is absorbed"

    assert "late arrival" in _comments(repo, tid), "doctor repair must restore visibility"
    assert not _orphan_findings(repo, tid), "doctor repair must clear ORPHAN_EVENT"
    assert _backup_refs(repo), "the repair must record a refs/rebar-doctor/ backup ref"


def test_doctor_repair_refuses_without_backup_ref(store: Path, monkeypatch) -> None:
    repo = store
    tid = _seed(repo, "no ref no mutation")
    orphan = _race_orphan(repo, tid)
    snaps_before = _snapshots(repo, tid)

    monkeypatch.setattr(_doctor, "_dirty_backup_ref", lambda _tracker: None)
    _doctor.doctor_cli(["--repair"], repo_root=str(repo))

    assert _snapshots(repo, tid) == snaps_before, "no backup ref => no mutation"
    assert orphan.exists()


# ── never re-absorb ──────────────────────────────────────────────────────────────────────────
def test_retired_cited_event_is_never_reabsorbed(store: Path) -> None:
    repo = store
    tid = _seed(repo, "retired stays retired")
    _race_orphan(repo, tid)
    retired_before = sorted(_tdir(repo, tid).glob("*.retired"))
    assert retired_before, "precondition: the first fold retired sources"

    assert _fold(repo, tid) == 0  # the absorbing re-fold

    bodies = _comments(repo, tid)
    assert bodies == ["c0", "c1", "c2", "late arrival"], (
        f"no folded comment may be duplicated or lost; got {bodies}"
    )
    for p in retired_before:
        assert p.exists(), f"retired source {p.name} must be untouched"


def test_live_event_cited_by_snapshot_is_not_double_applied(store: Path) -> None:
    """The SNAPSHOT_INCONSISTENT shape (a cited source still live) is fsck's territory:
    the absorbing re-fold re-cites it but never re-applies it."""
    repo = store
    tid = _seed(repo, "cited but live")
    _race_orphan(repo, tid)
    # Resurrect one cited, retired comment back to live.
    retired = sorted(
        p
        for p in _tdir(repo, tid).glob("*.retired")
        if json.loads(p.read_text()).get("event_type") == "COMMENT"
    )
    resurrected = retired[0]
    live_again = resurrected.with_name(resurrected.name[: -len(".retired")])
    live_again.write_text(resurrected.read_text(encoding="utf-8"), encoding="utf-8")

    assert _fold(repo, tid) == 0

    bodies = _comments(repo, tid)
    assert bodies.count("c0") == 1, f"a cited live event must not double-apply; got {bodies}"
    assert "late arrival" in bodies
