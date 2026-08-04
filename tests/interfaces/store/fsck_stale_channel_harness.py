"""Fixture builder shared by stale-channel fsck repair tests."""

from __future__ import annotations

import json
from pathlib import Path

_CREATE_UUID = "11111111-aaaa-4bbb-8ccc-000000000001"
_SNAP_UUID = "22222222-aaaa-4bbb-8ccc-000000000002"


def write_snapshot_ticket(
    tracker: Path,
    ticket_id: str,
    *,
    stale_channel: bool,
    orphan_type: str | None = None,
) -> tuple[Path, Path]:
    ticket_dir = tracker / ticket_id
    ticket_dir.mkdir(parents=True)
    create_name = f"1000000000000000000-{_CREATE_UUID}-CREATE.json.retired"
    (ticket_dir / create_name).write_text(
        json.dumps(
            {
                "event_type": "CREATE",
                "timestamp": 1000000000000000000,
                "uuid": _CREATE_UUID,
                "author": "test",
                "env_id": "test",
                "data": {
                    "id": ticket_id,
                    "ticket_type": "task",
                    "title": ticket_id,
                    "priority": 2,
                    "creation_channel": "python",
                },
            }
        ),
        encoding="utf-8",
    )
    compiled = {
        "id": ticket_id,
        "ticket_type": "task",
        "title": ticket_id,
        "priority": 2,
        "status": "open",
    }
    if not stale_channel:
        compiled["creation_channel"] = "python"
    if orphan_type is not None:
        orphan_uuid = "33333333-aaaa-4bbb-8ccc-000000000003"
        (ticket_dir / f"1500000000000000000-{orphan_uuid}-{orphan_type}.json").write_text(
            json.dumps({"event_type": orphan_type, "uuid": orphan_uuid}), encoding="utf-8"
        )
    snapshot = ticket_dir / f"2000000000000000000-{_SNAP_UUID}-SNAPSHOT.json"
    snapshot.write_text(
        json.dumps(
            {
                "event_type": "SNAPSHOT",
                "timestamp": 2000000000000000000,
                "uuid": _SNAP_UUID,
                "data": {"compiled_state": compiled, "source_event_uuids": [_CREATE_UUID]},
            }
        ),
        encoding="utf-8",
    )
    return ticket_dir, snapshot
