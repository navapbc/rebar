"""creation_channel durability through SNAPSHOT compaction (story 568c, epic jira-reb-977).

Compaction folds a ticket's events into one SNAPSHOT whose ``data.compiled_state``
must carry the exact ``creation_channel`` (+ true-only ``creation_channel_inferred``);
the SNAPSHOT envelope identifies the compactor, not the creator, so the channel never
appears there and is never stripped. SNAPSHOT-only replay, full-log rebuild, and
conservative-horizon compaction all reproduce the same provenance as full-log replay,
and a legacy SNAPSHOT that predates the field re-infers deterministically at read time.

Observable oracle only: SNAPSHOT.data.compiled_state, the SNAPSHOT envelope, and the
reduced (SNAPSHOT-only / rebuilt) ticket state.

``-k`` selectors: recorded, inferred, placement, rebuild, horizon, rollback_recovery.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar.reducer import reduce_ticket

_TS = 1742605200000000000  # a valid 19-digit HLC ns prefix
_UA = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
_UB = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _fold_everything(monkeypatch):
    # Fold the whole log on compaction (the standard lifecycle-test recipe).
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")


def _tdir(repo: Path, tid: str) -> Path:
    return repo / ".tickets-tracker" / tid


def _do_compact(repo: Path, tid: str, *, extra: list[str] | None = None) -> None:
    rc = _compact.compact_cli(
        [tid, "--threshold=0", "--skip-sync", *(extra or [])], repo_root=str(repo)
    )
    assert rc == 0, f"compact failed for {tid}"


def _snapshot_event(tdir: Path) -> dict:
    snaps = sorted(tdir.glob("*-SNAPSHOT.json"))
    assert len(snaps) == 1, f"expected exactly one SNAPSHOT, got {snaps}"
    return json.loads(snaps[0].read_text())


def _seed_foldable(repo: Path, title: str) -> str:
    tid = rebar.create_ticket("task", title, repo_root=str(repo))
    rebar.comment(tid, "c1", repo_root=str(repo))
    rebar.comment(tid, "c2", repo_root=str(repo))
    return tid


def _write_legacy_jira_ticket(repo: Path, tid: str = "jira-leg-1") -> str:
    """A pre-feature Jira ticket on disk: CREATE (no creation_channel) + a foldable
    COMMENT, both under the reconciler author/env (the legacy-Jira signature)."""
    tdir = _tdir(repo, tid)
    tdir.mkdir(parents=True)
    create = {
        "event_type": "CREATE",
        "timestamp": _TS,
        "uuid": _UA,
        "author": "reconciler",
        "env_id": "reconciler",
        "data": {"id": tid, "ticket_type": "task", "title": "legacy jira", "priority": 2},
    }
    comment = {
        "event_type": "COMMENT",
        "timestamp": _TS + 1,
        "uuid": _UB,
        "author": "reconciler",
        "env_id": "reconciler",
        "data": {"body": "legacy comment"},
    }
    (tdir / f"{_TS}-{_UA}-CREATE.json").write_text(json.dumps(create))
    (tdir / f"{_TS + 1}-{_UB}-COMMENT.json").write_text(json.dumps(comment))
    return tid


# ── recorded (AC1): a recorded channel survives into the snapshot + replay ────
def test_recorded_channel_survives_compaction(rebar_repo: Path):
    repo = rebar_repo
    tid = _seed_foldable(repo, "recorded")
    tdir = _tdir(repo, tid)
    _do_compact(repo, tid)
    snap = _snapshot_event(tdir)
    assert snap["data"]["compiled_state"]["creation_channel"] == "python"
    # SNAPSHOT-only replay (the CREATE is now retired) returns the recorded value, no marker.
    state = reduce_ticket(str(tdir))
    assert state["creation_channel"] == "python"
    assert "creation_channel_inferred" not in state


# ── placement (AC4): channel lives in compiled_state, never on the envelope ───
def test_placement_channel_in_compiled_state_not_envelope(rebar_repo: Path):
    strip = _compact._snapshot_strip_keys()
    assert "creation_channel" not in strip
    assert "creation_channel_inferred" not in strip
    tid = _seed_foldable(rebar_repo, "placement")
    _do_compact(rebar_repo, tid)
    snap = _snapshot_event(_tdir(rebar_repo, tid))
    assert "creation_channel" not in snap, "channel must not ride the SNAPSHOT envelope"
    assert "creation_channel_inferred" not in snap
    assert snap["data"]["compiled_state"]["creation_channel"] == "python"


# ── inferred (AC2): compacting a legacy Jira ticket materializes jira + marker ─
def test_inferred_jira_channel_materialized_by_compaction(rebar_repo: Path):
    tid = _write_legacy_jira_ticket(rebar_repo)
    _do_compact(rebar_repo, tid)
    snap = _snapshot_event(_tdir(rebar_repo, tid))
    compiled = snap["data"]["compiled_state"]
    assert compiled["creation_channel"] == "jira"
    assert compiled["creation_channel_inferred"] is True


# ── rebuild (AC5): full-log rebuild preserves the same provenance ─────────────
def test_rebuild_snapshot_preserves_creation_channel(rebar_repo: Path):
    repo = rebar_repo
    tid = _seed_foldable(repo, "rebuild")
    tdir = _tdir(repo, tid)
    _do_compact(repo, tid)
    # Rebuild from the full log (retained .retired CREATE) — provenance must persist.
    rebuilt = _compact.rebuild_snapshot_from_full_log(
        str(repo / ".tickets-tracker"), tid, str(tdir), no_commit=True
    )
    assert rebuilt is True
    assert _snapshot_event(tdir)["data"]["compiled_state"]["creation_channel"] == "python"
    assert reduce_ticket(str(tdir))["creation_channel"] == "python"


# ── horizon (AC5): conservative-horizon compaction preserves provenance ───────
def test_horizon_compaction_preserves_creation_channel(rebar_repo: Path, monkeypatch):
    # A non-zero horizon leaves the newest events "young" (unfolded) while the CREATE
    # folds into the SNAPSHOT — the channel must survive the mixed replay.
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", str(_TS))
    repo = rebar_repo
    tid = _seed_foldable(repo, "horizon")
    tdir = _tdir(repo, tid)
    _do_compact(repo, tid, extra=[f"--horizon={_TS}"])
    state = reduce_ticket(str(tdir))
    assert state["creation_channel"] == "python"
    assert "creation_channel_inferred" not in state


# ── rollback_recovery (AC5): a legacy SNAPSHOT missing the channel is DETECTED
#    (fsck SNAPSHOT_STALE_CHANNEL) and REPAIRED (--repair-snapshots rebuild). ─────
def test_rollback_recovery_detects_and_repairs_stale_channel_snapshot(rebar_repo: Path):
    from rebar._commands import fsck

    repo = rebar_repo
    tid = _write_legacy_jira_ticket(repo, "jira-leg-2")
    tdir = _tdir(repo, tid)
    _do_compact(repo, tid)
    # Simulate a pre-feature SNAPSHOT: strip creation_channel from its compiled_state.
    snap_path = sorted(tdir.glob("*-SNAPSHOT.json"))[0]
    snap = json.loads(snap_path.read_text())
    snap["data"]["compiled_state"].pop("creation_channel", None)
    snap["data"]["compiled_state"].pop("creation_channel_inferred", None)
    snap_path.write_text(json.dumps(snap))

    # Read-time re-inference already keeps SNAPSHOT-only replay correct.
    assert reduce_ticket(str(tdir))["creation_channel"] == "jira"

    # DETECT: fsck surfaces SNAPSHOT_STALE_CHANNEL (compiled_state lacks the key AND a
    # retained CREATE exists), so the DURABLE snapshot is flagged as rebuildable.
    findings = fsck._check_snapshot(str(tdir), tid, snap_path.name)
    assert any("SNAPSHOT_STALE_CHANNEL" in f for f in findings), findings

    # REPAIR: --repair-snapshots rebuilds the snapshot from the retained CREATE, baking
    # the inferred provenance back into compiled_state.
    fsck.fsck_cli(["--repair-snapshots"], repo_root=str(repo))
    new_path = sorted(tdir.glob("*-SNAPSHOT.json"))[0]
    rebuilt = json.loads(new_path.read_text())["data"]["compiled_state"]
    assert rebuilt["creation_channel"] == "jira"
    assert rebuilt["creation_channel_inferred"] is True
    # ...and the stale-channel finding clears after the repair.
    assert not any(
        "SNAPSHOT_STALE_CHANNEL" in f for f in fsck._check_snapshot(str(tdir), tid, new_path.name)
    )
