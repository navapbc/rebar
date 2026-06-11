"""Shared helpers for the reducer test split.

Formerly inline at the top of tests/scripts/test_ticket_reducer.py; extracted so
the seam-split test files (test_reducer_*.py) share one definition instead of
duplicating it. The module-under-test fixture (`reducer`) lives in conftest.py.
"""

from __future__ import annotations

import json
from pathlib import Path

# reducer/_events.py -> reducer -> scripts -> tests -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[3]

_UUID = "3f2a1b4c-5e6d-7f8a-9b0c-1d2e3f4a5b6c"
_UUID2 = "aabbccdd-1122-3344-5566-778899aabbcc"
_UUID3 = "deadbeef-dead-beef-dead-beefdeadbeef"


def _write_event(
    ticket_dir: Path,
    timestamp: int,
    uuid: str,
    event_type: str,
    data: dict,
    env_id: str = "00000000-0000-4000-8000-000000000001",
    author: str = "Test User",
) -> Path:
    """Write a well-formed event JSON file and return its path."""
    filename = f"{timestamp}-{uuid}-{event_type}.json"
    payload = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": env_id,
        "author": author,
        "data": data,
    }
    path = ticket_dir / filename
    path.write_text(json.dumps(payload))
    return path
