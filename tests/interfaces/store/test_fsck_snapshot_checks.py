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
