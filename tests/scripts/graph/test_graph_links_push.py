"""Same-second LINK/UNLINK ordering and _write_link_event push-retry

Split from the former monolithic tests/scripts/test_ticket_graph.py along
graph-concern seams. The `graph` fixture + autouse git-isolation fixture live in
conftest.py; event-writing helpers + the module loader in _helpers.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from _helpers import (
    REPO_ROOT,
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
    """_is_active_link correctly handles LINK+UNLINK events that share the same Unix-second timestamp.

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
    result = graph._is_active_link(
        "src-ticket", "tgt-ticket", "blocks", str(tracker_dir)
    )
    assert result is False, (
        "_is_active_link returned True but the link was cancelled by an UNLINK event. "
        "This indicates same-second UNLINK is sorting before LINK — the timestamp "
        "tie-breaker (event_type_order: LINK=0, UNLINK=1) is missing or incorrect."
    )


# ---------------------------------------------------------------------------
# Tests for _write_link_event push retry logic (bug 79de-85d4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_write_link_event_retries_on_non_fast_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_write_link_event retries push on non-fast-forward and succeeds on second attempt.

    Mock ordering note: subprocess.run is called for git add, git commit, git remote
    BEFORE the push retry loop, so side_effects must account for all 7 calls:
      add_ok, commit_ok, remote_ok, push_fail, fetch_ok, rebase_ok, push_ok
    MagicMock does NOT simulate check=True raising CalledProcessError — returncode is
    returned but no exception is raised for add/commit mocks.
    """
    import sys
    from unittest.mock import MagicMock, patch

    _scripts_dir = str(REPO_ROOT / "src" / "rebar" / "_engine")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

    from rebar.graph._links import _write_link_event as _real_write_link_event

    # Sandbox cwd to tmp_path so any relative file write under test stays inside
    # the auto-cleaned fixture rather than landing in REPO_ROOT.
    monkeypatch.chdir(tmp_path)

    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    (tracker_dir / "tkt-src3").mkdir()

    ok = MagicMock(returncode=0, stdout="", stderr="")
    remote_ok = MagicMock(returncode=0, stdout="origin\n", stderr="")
    push_fail = MagicMock(
        returncode=1, stdout="", stderr="error: non-fast-forward updates were rejected"
    )
    push_ok = MagicMock(returncode=0, stdout="", stderr="")

    # Call order: git add, git commit, git remote, git push (fail),
    #             git fetch, git rebase, git push (success)
    side_effects = [ok, ok, remote_ok, push_fail, ok, ok, push_ok]

    with (
        patch("subprocess.run", side_effect=side_effects) as mock_run,
        patch.dict("os.environ", {"_TICKET_TEST_NO_SYNC": ""}, clear=False),
    ):
        _real_write_link_event("tkt-src3", "tkt-tgt3", "depends_on", str(tracker_dir))

    all_cmds = [c.args[0] for c in mock_run.call_args_list if c.args]
    push_calls = [cmd for cmd in all_cmds if "push" in cmd]
    assert len(push_calls) == 2, (
        f"Expected 2 push attempts (1 non-fast-forward + 1 retry success), got {len(push_calls)}. "
        f"All commands: {all_cmds}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_write_link_event_push_gives_up_on_merge_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_write_link_event is best-effort: if rebase AND merge both fail, it gives up silently."""
    import sys
    from unittest.mock import MagicMock, patch

    _scripts_dir = str(REPO_ROOT / "src" / "rebar" / "_engine")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

    from rebar.graph._links import _write_link_event as _real_write_link_event

    # Sandbox cwd to tmp_path so any relative file write under test stays inside
    # the auto-cleaned fixture rather than landing in REPO_ROOT.
    monkeypatch.chdir(tmp_path)

    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    (tracker_dir / "tkt-src4").mkdir()

    ok = MagicMock(returncode=0, stdout="", stderr="")
    remote_ok = MagicMock(returncode=0, stdout="origin\n", stderr="")
    push_fail = MagicMock(
        returncode=1, stdout="", stderr="error: non-fast-forward updates were rejected"
    )
    rebase_fail = MagicMock(
        returncode=1, stdout="", stderr="CONFLICT (content): Merge conflict"
    )
    merge_fail = MagicMock(
        returncode=1, stdout="", stderr="CONFLICT (content): Merge conflict"
    )

    # git add, git commit, git remote, git push (fail), git fetch,
    # git rebase (fail), git rebase --abort, git merge (fail), git merge --abort
    side_effects = [ok, ok, remote_ok, push_fail, ok, rebase_fail, ok, merge_fail, ok]

    with (
        patch("subprocess.run", side_effect=side_effects),
        patch.dict("os.environ", {"_TICKET_TEST_NO_SYNC": ""}, clear=False),
    ):
        # Must not raise — best-effort means failure is silently swallowed
        _real_write_link_event("tkt-src4", "tkt-tgt4", "depends_on", str(tracker_dir))
