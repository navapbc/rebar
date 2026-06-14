"""Shared helpers for the ticket-graph test split.

Extracted from the former monolithic tests/scripts/test_ticket_graph.py so the
seam-split files share one definition of the event-writing helpers + the module
loader, instead of duplicating them (``_write_ticket`` alone has 145 call sites).
The `graph` fixture + autouse git-isolation fixture live in conftest.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

# graph/_helpers.py -> graph -> scripts -> tests -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[3]

_UUID_A = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
_UUID_C = "cccccccc-cccc-4ccc-cccc-cccccccccccc"
_UUID_D = "dddddddd-dddd-4ddd-dddd-dddddddddddd"


def _load_module() -> ModuleType:
    """Return the canonical graph package (Tier E E7d: was the hyphenated
    ticket-graph.py CLI wrapper, which only re-exported rebar.graph)."""
    import rebar.graph

    return rebar.graph

def _write_ticket(
    tracker_dir: Path,
    ticket_id: str,
    status: str = "open",
    parent_id: str | None = None,
    ticket_type: str = "task",
) -> Path:
    """Write a minimal ticket directory with a CREATE event and optional STATUS event.

    Returns the ticket directory path.
    """
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
            "data": {
                "status": status,
                "current_status": "open",
            },
        }
        with open(ticket_dir / f"2000-status-{ticket_id}-STATUS.json", "w") as f:
            json.dump(status_event, f)

    return ticket_dir

def _write_blocks_link(
    tracker_dir: Path,
    blocker_id: str,
    blocked_id: str,
    link_uuid: str | None = None,
    timestamp: int = 1500,
) -> None:
    """Write a LINK event in blocker_id's directory: blocker_id blocks blocked_id.

    Follows the schema used by ticket-link.sh: LINK event is stored in the
    blocker's directory with data.target_id=blocked_id and data.relation='blocks'.
    """
    if link_uuid is None:
        link_uuid = f"link-{blocker_id}-blocks-{blocked_id}"
    blocker_dir = tracker_dir / blocker_id
    blocker_dir.mkdir(parents=True, exist_ok=True)
    link_event = {
        "event_type": "LINK",
        "uuid": link_uuid,
        "timestamp": timestamp,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "target_id": blocked_id,
            "relation": "blocks",
        },
    }
    filename = f"{timestamp}-{link_uuid}-LINK.json"
    with open(blocker_dir / filename, "w") as f:
        json.dump(link_event, f)

def _write_archive_event(
    tracker_dir: Path, ticket_id: str, timestamp: int = 3000
) -> None:
    """Write an ARCHIVED event to ticket_id's directory.

    This marks the ticket as archived in the event-sourced state.
    The ticket-reducer.py handles ARCHIVED events by setting state['archived'] = True.
    """
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    archive_event = {
        "event_type": "ARCHIVED",
        "uuid": f"archive-{ticket_id}",
        "timestamp": timestamp,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {},
    }
    with open(ticket_dir / f"{timestamp}-archive-{ticket_id}-ARCHIVED.json", "w") as f:
        json.dump(archive_event, f)

def _make_ticket(tracker: Path, ticket_id: str, ticket_type: str = "task") -> Path:
    """Write a minimal ticket directory with a CREATE event. Returns the ticket dir."""
    ticket_dir = tracker / ticket_id
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
            "parent_id": None,
        },
    }
    with open(ticket_dir / f"1000-create-{ticket_id}-CREATE.json", "w") as f:
        json.dump(create_event, f)
    return ticket_dir

def _write_link_event(
    source_id: str,
    target_id: str,
    relation: str,
    tracker_dir: str,
    timestamp: int = 1500,
) -> None:
    """Write a LINK event in source_id's directory pointing at target_id."""
    source_dir = Path(tracker_dir) / source_id
    source_dir.mkdir(parents=True, exist_ok=True)
    link_uuid = f"link-{source_id}-{relation}-{target_id}"
    link_event = {
        "event_type": "LINK",
        "uuid": link_uuid,
        "timestamp": timestamp,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "target_id": target_id,
            "relation": relation,
        },
    }
    filename = f"{timestamp}-{link_uuid}-LINK.json"
    with open(source_dir / filename, "w") as f:
        json.dump(link_event, f)

def _get_check_cycle_at_level():  # type: ignore[no-untyped-def]
    """Load check_cycle_at_level from ticket-graph module."""
    mod = _load_module()
    return mod.check_cycle_at_level
