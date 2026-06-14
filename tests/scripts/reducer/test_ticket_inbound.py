"""RED tests for inbound-relationship derivation (ticket show completeness).

These tests are RED — they exercise functionality that does not yet exist.
The module under test is expected to expose, from the ``ticket_reducer``
package::

    find_inbound_relationships(ticket_id: str, tracker_dir: str) -> dict

Contract:
  - Returns ``{"ticket_id": str, "inbound_links": list, "children": list}``.
  - ``inbound_links`` is a sorted list of ``{"from_id": str, "relation": str}``
    for every *other* ticket whose net-active LINK event targets ``ticket_id``.
  - ``children`` is a sorted list of ticket IDs whose ``parent_id == ticket_id``.
  - The subject ticket never appears in its own inbound results.
  - Source tickets in terminal ``deleted`` state are not surfaced.
  - A reciprocal ``relates_to`` already present on the subject's own outgoing
    deps is NOT duplicated into ``inbound_links``.
  - Candidates are pre-filtered to tickets whose event files mention the ID; a
    ticket that merely mentions the ID in prose (e.g. a comment) but holds no
    structured link/parent to it is NOT reported.

Run: python3 -m pytest tests/scripts/test_ticket_inbound.py -x
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# 16-hex-style canonical IDs (xxxx-xxxx-xxxx-xxxx) — match the production ID
# shape so substring prefiltering behaves exactly as it would in real corpora.
ID_A = "aaaa-aaaa-aaaa-aaaa"
ID_B = "bbbb-bbbb-bbbb-bbbb"
ID_C = "cccc-cccc-cccc-cccc"
ID_D = "dddd-dddd-dddd-dddd"


@pytest.fixture()
def inbound():
    """Import find_inbound_relationships, failing (RED) until it exists."""
    try:
        from rebar.reducer import find_inbound_relationships  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - RED guard
        pytest.fail(
            "ticket_reducer.find_inbound_relationships not importable — "
            f"expected RED until implemented: {exc}"
        )
    return find_inbound_relationships


def _write_ticket(
    tracker_dir: Path,
    ticket_id: str,
    status: str = "open",
    parent_id: str | None = None,
    ticket_type: str = "task",
) -> Path:
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    create_event = {
        "event_type": "CREATE",
        "uuid": f"create-{ticket_id}",
        "timestamp": 1000,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": ticket_type,
            "title": f"Ticket {ticket_id}",
            "parent_id": parent_id,
        },
    }
    with open(ticket_dir / f"1000-create-{ticket_id}-CREATE.json", "w") as f:
        json.dump(create_event, f)
    if status != "open":
        status_event = {
            "event_type": "STATUS",
            "uuid": f"status-{ticket_id}",
            "timestamp": 2000,
            "author": "Test User",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "data": {"status": status, "current_status": "open"},
        }
        with open(ticket_dir / f"2000-status-{ticket_id}-STATUS.json", "w") as f:
            json.dump(status_event, f)
    return ticket_dir


def _write_link(
    tracker_dir: Path,
    source_id: str,
    target_id: str,
    relation: str,
    timestamp: int = 1500,
) -> None:
    """Write a LINK event into source_id's dir targeting target_id."""
    source_dir = tracker_dir / source_id
    source_dir.mkdir(parents=True, exist_ok=True)
    link_uuid = f"link-{source_id}-{relation}-{target_id}"
    link_event = {
        "event_type": "LINK",
        "uuid": link_uuid,
        "timestamp": timestamp,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {"target_id": target_id, "relation": relation},
    }
    with open(source_dir / f"{timestamp}-{link_uuid}-LINK.json", "w") as f:
        json.dump(link_event, f)


def _write_comment(
    tracker_dir: Path, ticket_id: str, body: str, timestamp: int = 1700
) -> None:
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    comment_event = {
        "event_type": "COMMENT",
        "uuid": f"comment-{ticket_id}-{timestamp}",
        "timestamp": timestamp,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {"body": body},
    }
    with open(ticket_dir / f"{timestamp}-comment-{ticket_id}-COMMENT.json", "w") as f:
        json.dump(comment_event, f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_inbound_blocks(inbound, tmp_path: Path) -> None:
    """A 'blocks' link from A→B surfaces as an inbound link on B."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A)
    _write_ticket(tracker, ID_B)
    _write_link(tracker, ID_A, ID_B, "blocks")

    result = inbound(ID_B, str(tracker))

    assert {"from_id": ID_A, "relation": "blocks"} in result["inbound_links"], (
        f"Expected A→B blocks inbound on B, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_inbound_depends_on(inbound, tmp_path: Path) -> None:
    """A 'depends_on' link from A→B surfaces as an inbound link on B."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A)
    _write_ticket(tracker, ID_B)
    _write_link(tracker, ID_A, ID_B, "depends_on")

    result = inbound(ID_B, str(tracker))

    assert {"from_id": ID_A, "relation": "depends_on"} in result["inbound_links"]


@pytest.mark.unit
@pytest.mark.scripts
def test_children_via_parent(inbound, tmp_path: Path) -> None:
    """Tickets whose parent_id == subject are reported as children."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A, ticket_type="epic")
    _write_ticket(tracker, ID_B, parent_id=ID_A)
    _write_ticket(tracker, ID_C, parent_id=ID_A)

    result = inbound(ID_A, str(tracker))

    assert result["children"] == sorted([ID_B, ID_C]), (
        f"Expected children [{ID_B}, {ID_C}], got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_no_inbound_for_isolated_ticket(inbound, tmp_path: Path) -> None:
    """A ticket nobody references has empty inbound_links and children."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A)
    _write_ticket(tracker, ID_B)  # unrelated, no link to A

    result = inbound(ID_A, str(tracker))

    assert result["inbound_links"] == []
    assert result["children"] == []


@pytest.mark.unit
@pytest.mark.scripts
def test_subject_excluded_from_own_inbound(inbound, tmp_path: Path) -> None:
    """A ticket's own outgoing link does not appear as inbound to itself."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A)
    _write_ticket(tracker, ID_B)
    _write_link(tracker, ID_A, ID_B, "blocks")

    result = inbound(ID_A, str(tracker))

    assert all(e["from_id"] != ID_A for e in result["inbound_links"])


@pytest.mark.unit
@pytest.mark.scripts
def test_relates_to_not_duplicated(inbound, tmp_path: Path) -> None:
    """A reciprocal relates_to already on the subject's own deps is suppressed."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A)
    _write_ticket(tracker, ID_B)
    # Bidirectional relates_to (as ticket_link writes it): both dirs hold a LINK.
    _write_link(tracker, ID_A, ID_B, "relates_to")
    _write_link(tracker, ID_B, ID_A, "relates_to")

    result = inbound(ID_A, str(tracker))

    relates = [e for e in result["inbound_links"] if e["relation"] == "relates_to"]
    assert relates == [], (
        "Reciprocal relates_to (already in A's own deps) must not be duplicated "
        f"into inbound_links, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_deleted_source_excluded(inbound, tmp_path: Path) -> None:
    """A blocks link from a deleted ticket is not surfaced as a live inbound link."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A, status="deleted")
    _write_ticket(tracker, ID_B)
    _write_link(tracker, ID_A, ID_B, "blocks")

    result = inbound(ID_B, str(tracker))

    assert all(e["from_id"] != ID_A for e in result["inbound_links"]), (
        f"Deleted source {ID_A} must not appear as an inbound link, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_archived_source_excluded(inbound, tmp_path: Path) -> None:
    """A blocks link from an archived ticket is not surfaced as a live inbound link.

    This exercises the ``state.get("archived")`` exclusion branch specifically —
    distinct from the ``status in {"deleted"}`` branch. An ARCHIVED event sets
    status to ``"archived"`` (not ``"deleted"``), so exclusion here can only come
    from the archived-flag check, not from ``_INACTIVE_SOURCE_STATUSES``.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A)
    _write_ticket(tracker, ID_B)
    _write_link(tracker, ID_A, ID_B, "blocks")
    # ARCHIVED event on the source — process_archived sets archived=True and
    # status="archived" (data is unread by the processor).
    archived_event = {
        "event_type": "ARCHIVED",
        "uuid": "archived-A",
        "timestamp": 1800,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {},
    }
    with open(tracker / ID_A / "1800-archived-A-ARCHIVED.json", "w") as f:
        json.dump(archived_event, f)

    result = inbound(ID_B, str(tracker))

    assert all(e["from_id"] != ID_A for e in result["inbound_links"]), (
        f"Archived source {ID_A} must not appear as an inbound link, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_prose_mention_not_reported(inbound, tmp_path: Path) -> None:
    """A ticket that only mentions the ID in a comment (no structured link) is dropped."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A)
    _write_ticket(tracker, ID_B)
    # B's comment names A but B holds no link to A and is not A's child.
    _write_comment(tracker, ID_B, body=f"see also {ID_A} for context")

    result = inbound(ID_A, str(tracker))

    assert all(e["from_id"] != ID_B for e in result["inbound_links"])
    assert ID_B not in result["children"]


@pytest.mark.unit
@pytest.mark.scripts
def test_unlinked_source_not_reported(inbound, tmp_path: Path) -> None:
    """A link cancelled by a later UNLINK is not surfaced as inbound."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, ID_A)
    _write_ticket(tracker, ID_B)
    _write_link(tracker, ID_A, ID_B, "blocks", timestamp=1500)
    # UNLINK cancelling the LINK above (matches reducer net-effective semantics).
    unlink_event = {
        "event_type": "UNLINK",
        "uuid": "unlink-A-B",
        "timestamp": 1600,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {"link_uuid": f"link-{ID_A}-blocks-{ID_B}", "target_id": ID_B},
    }
    with open(tracker / ID_A / "1600-unlink-A-B-UNLINK.json", "w") as f:
        json.dump(unlink_event, f)

    result = inbound(ID_B, str(tracker))

    assert all(e["from_id"] != ID_A for e in result["inbound_links"]), (
        f"Unlinked source must not be surfaced, got {result!r}"
    )
