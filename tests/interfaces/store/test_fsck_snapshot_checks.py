"""fsck snapshot invariants: SNAPSHOT_INCONSISTENT and ORPHAN_EVENT.

Regression coverage for the ORPHAN_EVENT false positive (bug saggy-pupil-plant /
f193-223f-eb36-4061, RC2a): compaction deliberately folds ONLY ``KNOWN_EVENT_TYPES``
(``_commands/compact.py`` excludes any ``event_type not in KNOWN_EVENT_TYPES`` from
both deletion and ``source_event_uuids``). A reducer-ignored type such as
``REVIEW_RESULT`` (in ``_NON_REPLAY_KNOWN_TYPES``, not in ``KNOWN_EVENT_TYPES``) that
predates a snapshot is therefore *correctly* absent from ``source_event_uuids`` — it
must NOT be flagged ORPHAN_EVENT. The check must stay symmetric with compaction:
exempt exactly the types compaction never folds, while still flagging a genuinely
orphaned KNOWN-type event (real data loss, RC2b) and still flagging
SNAPSHOT_INCONSISTENT.

Unit-level: drives ``fsck._check_snapshot`` directly against a crafted ticket dir.
"""

from __future__ import annotations

import json
from pathlib import Path

from rebar._commands import fsck


def _write_event(ticket_dir: Path, timestamp: str, uuid: str, etype: str) -> str:
    """Write a minimal event file named ``{timestamp}-{uuid}-{etype}.json`` and
    return the filename. ``_check_snapshot`` keys off the filename, not contents."""
    name = f"{timestamp}-{uuid}-{etype}.json"
    (ticket_dir / name).write_text(
        json.dumps({"uuid": uuid, "event_type": etype}), encoding="utf-8"
    )
    return name


def _write_snapshot(ticket_dir: Path, timestamp: str, uuid: str, source_uuids: list[str]) -> str:
    name = f"{timestamp}-{uuid}-SNAPSHOT.json"
    (ticket_dir / name).write_text(
        json.dumps(
            {"uuid": uuid, "event_type": "SNAPSHOT", "data": {"source_event_uuids": source_uuids}}
        ),
        encoding="utf-8",
    )
    return name


def test_orphan_check_exempts_reducer_ignored_types(tmp_path: Path) -> None:
    """A pre-snapshot REVIEW_RESULT (non-KNOWN, never folded by compaction) must
    NOT be reported as ORPHAN_EVENT. This is the RC2a false positive."""
    td = tmp_path / "reb-1"
    td.mkdir()
    # KNOWN CREATE was folded + deleted (present only in source_event_uuids).
    create_uuid = "c0c0c0c0-1111-2222-3333-444444444444"
    # A REVIEW_RESULT predates the snapshot, on disk, and (correctly) not folded.
    rr_uuid = "d1d1d1d1-5555-6666-7777-888888888888"
    _write_event(td, "1001000000000000000", rr_uuid, "REVIEW_RESULT")
    snap = _write_snapshot(
        td, "2000000000000000000", "e2e2e2e2-9999-0000-1111-222222222222", [create_uuid]
    )

    out = fsck._check_snapshot(str(td), "reb-1", snap)

    assert not any("ORPHAN_EVENT" in line for line in out), (
        f"REVIEW_RESULT (reducer-ignored, never folded by compaction) must not be "
        f"flagged ORPHAN_EVENT; got:\n{out}"
    )


def test_orphan_check_still_flags_known_type_orphan(tmp_path: Path) -> None:
    """Teeth / anti-over-suppression: a genuinely orphaned KNOWN-type event (a
    COMMENT predating the snapshot, not in source_event_uuids) IS real data loss
    (RC2b) and must still be reported."""
    td = tmp_path / "reb-2"
    td.mkdir()
    # A folded KNOWN event (deleted, present only in source_event_uuids) keeps the
    # source list non-empty so the checks run.
    folded_uuid = "f0f0f0f0-1111-2222-3333-444444444444"
    orphan_comment_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
    _write_event(td, "1001000000000000000", orphan_comment_uuid, "COMMENT")
    snap = _write_snapshot(
        td, "2000000000000000000", "bbbbbbbb-9999-0000-1111-222222222222", [folded_uuid]
    )

    out = fsck._check_snapshot(str(td), "reb-2", snap)

    assert any("ORPHAN_EVENT" in line and orphan_comment_uuid in line for line in out), (
        f"A KNOWN-type pre-snapshot event absent from source_event_uuids must still "
        f"be flagged ORPHAN_EVENT (real data loss); got:\n{out}"
    )


def test_snapshot_inconsistent_still_flags_undeleted_source(tmp_path: Path) -> None:
    """Regression guard: a source UUID whose file still exists is still
    SNAPSHOT_INCONSISTENT (the fix must not weaken this check)."""
    td = tmp_path / "reb-3"
    td.mkdir()
    src_uuid = "ffffffff-1111-2222-3333-444444444444"
    _write_event(td, "1001000000000000000", src_uuid, "EDIT")
    snap = _write_snapshot(
        td, "2000000000000000000", "cccccccc-9999-0000-1111-222222222222", [src_uuid]
    )

    out = fsck._check_snapshot(str(td), "reb-3", snap)

    assert any("SNAPSHOT_INCONSISTENT" in line and src_uuid in line for line in out), (
        f"A source UUID whose event file still exists must be SNAPSHOT_INCONSISTENT; got:\n{out}"
    )


# ─────────────────── A3 (34b1) live-store remediation ─────────────────────────
from rebar.reducer import KNOWN_EVENT_TYPES  # noqa: E402

_S1 = "11111111-aaaa-bbbb-cccc-000000000001"
_S2 = "22222222-aaaa-bbbb-cccc-000000000002"
_SNAP = "99999999-aaaa-bbbb-cccc-000000000009"


def test_orphan_disposition_covers_all_known_event_types() -> None:
    """The AUTO-RECOVER / HUMAN-TRIAGE disposition must classify EVERY orphan-eligible
    KNOWN_EVENT_TYPE (all but CREATE/SNAPSHOT), with no overlap and no leftovers."""
    auto, triage = fsck._AUTO_RECOVER_ORPHAN_TYPES, fsck._HUMAN_TRIAGE_ORPHAN_TYPES
    assert auto.isdisjoint(triage), "an event type is in both dispositions"
    assert auto | triage == set(KNOWN_EVENT_TYPES) - {"CREATE", "SNAPSHOT"}


def test_repair_plan_marks_still_present_source_for_retire(tmp_path: Path) -> None:
    """SNAPSHOT_INCONSISTENT: a folded source still present as an active file is queued
    for retire (rename to .retired), NOT a rebuild."""
    td = tmp_path / "reb-a3-1"
    td.mkdir()
    src = _write_event(td, "1000000000000000000", _S1, "COMMENT")  # listed AND still present
    _write_snapshot(td, "2000000000000000000", _SNAP, [_S1])

    plan = fsck._repair_plan(str(td), "reb-a3-1")
    assert plan["retire"] == [src]
    assert plan["auto_orphans"] == [] and plan["triage_orphans"] == []


def test_repair_plan_routes_orphans_by_type(tmp_path: Path) -> None:
    """A pre-snapshot orphan (absent from source_event_uuids) is routed by type:
    additive → AUTO-RECOVER, order-sensitive → HUMAN-TRIAGE."""
    td = tmp_path / "reb-a3-2"
    td.mkdir()
    auto = _write_event(td, "1000000000000000000", _S1, "COMMENT")  # additive orphan
    triage = _write_event(td, "1000000000000000001", _S2, "STATUS")  # order-sensitive orphan
    _write_snapshot(td, "2000000000000000000", _SNAP, [])  # folds nothing → both are orphans

    plan = fsck._repair_plan(str(td), "reb-a3-2")
    assert (auto, "COMMENT") in plan["auto_orphans"]
    assert (triage, "STATUS") in plan["triage_orphans"]
    assert auto not in [n for n, _ in plan["triage_orphans"]]


def test_repair_plan_retires_folded_older_snapshot(tmp_path: Path) -> None:
    """SNAPSHOT_INCONSISTENT sub-case (found in the A3 live run): a re-compaction folds a
    prior SNAPSHOT into the newer one's source_event_uuids. The still-present older
    snapshot must be queued for retire (like any folded source) — NOT excluded merely for
    being a ``-SNAPSHOT.json`` — while the latest snapshot (the horizon) is never touched
    and the older snapshot is never mis-classified as an orphan."""
    td = tmp_path / "reb-a3-snapfold"
    td.mkdir()
    older_snap = _write_snapshot(td, "1000000000000000000", _S1, [])  # still present, folded
    newer_snap = _write_snapshot(td, "2000000000000000000", _S2, [_S1])  # folds the older snap

    plan = fsck._repair_plan(str(td), "reb-a3-snapfold")
    assert plan["retire"] == [older_snap], plan
    assert newer_snap not in plan["retire"], "the latest snapshot (horizon) must never retire"
    assert plan["auto_orphans"] == [] and plan["triage_orphans"] == []


def test_repair_dry_run_makes_no_writes(tmp_path: Path) -> None:
    """--dry-run describes the repair but renames nothing."""
    td = tmp_path / "reb-a3-3"
    td.mkdir()
    src = _write_event(td, "1000000000000000000", _S1, "COMMENT")
    _write_snapshot(td, "2000000000000000000", _SNAP, [_S1])
    before = {p.name for p in td.iterdir()}

    disp = fsck._repair_ticket(str(tmp_path), "reb-a3-3", str(td), dry_run=True)

    assert disp["retired"] == [src]
    assert {p.name for p in td.iterdir()} == before, "dry-run must not touch the store"
    assert (td / src).exists() and not (td / (src + ".retired")).exists()


def test_repair_retires_still_present_source_live(tmp_path: Path) -> None:
    """A live repair renames the still-present folded source to *.retired, resolving
    SNAPSHOT_INCONSISTENT without a rebuild."""
    td = tmp_path / "reb-a3-4"
    td.mkdir()
    src = _write_event(td, "1000000000000000000", _S1, "COMMENT")
    snap = _write_snapshot(td, "2000000000000000000", _SNAP, [_S1])

    assert any("SNAPSHOT_INCONSISTENT" in line for line in fsck._check_snapshot(str(td), "x", snap))

    disp = fsck._repair_ticket(str(tmp_path), "reb-a3-4", str(td), dry_run=False)

    assert disp["retired"] == [src] and disp["rebuilt"] is False
    assert not (td / src).exists() and (td / (src + ".retired")).exists()
    assert not any(
        "SNAPSHOT_INCONSISTENT" in line for line in fsck._check_snapshot(str(td), "x", snap)
    )
