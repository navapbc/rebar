"""Same-second LINK/UNLINK ordering.

Split from the former monolithic tests/scripts/test_ticket_graph.py along
graph-concern seams. The `graph` fixture + autouse git-isolation fixture live in
conftest.py; event-writing helpers + the module loader in _helpers.py.

Note: the former ``_write_link_event`` push-retry tests were removed when graph
link writes were routed through the canonical store path
(``rebar._store.event_append.write_and_push``) — the hand-rolled flock+git+push-retry
loop they exercised no longer exists, and push-retry/non-fast-forward behavior is
now owned and tested at the store layer (``rebar._store.push``). See epic
``clumsy-jab-yacht`` / story ``scabby-slur-junk``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from _helpers import (
    _write_ticket,
)

# ---------------------------------------------------------------------------
# Same-second LINK/UNLINK timestamp ordering — _is_active_link must not allow
# UNLINK to replay before LINK when they share the same Unix-second timestamp
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_is_active_link_same_second_unlink_sorts_after_link(
    graph: ModuleType, tmp_path: Path
) -> None:
    """_is_active_link correctly handles LINK+UNLINK events that share the same Unix-second
    timestamp.

    When a LINK and its cancelling UNLINK share the same timestamp second but have
    different random UUIDs, a pure alphabetic filename sort can place the UNLINK before
    the LINK — making the link appear active when it has been cancelled.

    This test crafts filenames where the UNLINK UUID sorts alphabetically before the LINK UUID
    at the same timestamp, directly exercising the sort-order bug.

    Expected: _is_active_link returns False (link is net-inactive after the UNLINK).
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "src-ticket", status="open")
    _write_ticket(tracker_dir, "tgt-ticket", status="open")

    src_dir = tracker_dir / "src-ticket"

    # The link UUID embedded in the LINK event (and referenced by UNLINK's link_uuid)
    link_uuid = "ffffffff-ffff-4fff-ffff-ffffffffffff"
    # UNLINK UUID starts with '00000000...' → sorts before LINK UUID alphabetically
    unlink_uuid = "00000000-0000-4000-8000-000000000000"
    same_ts = 1000000000

    # Craft filenames so UNLINK sorts before LINK at the same timestamp
    #   UNLINK: "1000000000-00000000-...-UNLINK.json"   ← sorts first alphabetically
    #   LINK:   "1000000000-ffffffff-...-LINK.json"     ← sorts second alphabetically
    link_filename = f"{same_ts}-{link_uuid}-LINK.json"
    unlink_filename = f"{same_ts}-{unlink_uuid}-UNLINK.json"

    # Verify our crafted names actually produce the bad sort order (pre-condition)
    assert unlink_filename < link_filename, (
        "Pre-condition failed: UNLINK filename must sort before LINK filename to exercise the bug. "
        f"Got unlink={unlink_filename!r}, link={link_filename!r}"
    )

    # Write LINK event (link_uuid in 'uuid' field)
    link_event = {
        "event_type": "LINK",
        "uuid": link_uuid,
        "timestamp": same_ts,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "target_id": "tgt-ticket",
            "relation": "blocks",
        },
    }
    with open(src_dir / link_filename, "w") as f:
        json.dump(link_event, f)

    # Write UNLINK event (references link_uuid via data.link_uuid)
    unlink_event = {
        "event_type": "UNLINK",
        "uuid": unlink_uuid,
        "timestamp": same_ts,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "link_uuid": link_uuid,
            "target_id": "tgt-ticket",
            "relation": "blocks",
        },
    }
    with open(src_dir / unlink_filename, "w") as f:
        json.dump(unlink_event, f)

    # _is_active_link must return False: the UNLINK cancels the LINK, net state = inactive
    # With the bug: returns True (UNLINK replayed before LINK → LINK appears active again)
    # With the fix: returns False (LINK always replays before UNLINK at same timestamp)
    result = graph._is_active_link("src-ticket", "tgt-ticket", "blocks", str(tracker_dir))
    assert result is False, (
        "_is_active_link returned True but the link was cancelled by an UNLINK event. "
        "This indicates same-second UNLINK is sorting before LINK — the timestamp "
        "tie-breaker (event_type_order: LINK=0, UNLINK=1) is missing or incorrect."
    )
