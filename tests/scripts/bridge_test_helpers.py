"""Shared constants and helpers for bridge field-coverage tests.

This module contains non-fixture code (constants, helper functions) that test
files import directly.  Pytest fixtures remain in conftest.py (auto-discovered
by pytest).  Keeping helpers here avoids the fragile ``from conftest import``
anti-pattern, which breaks when pytest is invoked from a directory where
``tests/scripts/`` is not on ``sys.path``.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BRIDGE_ENV_ID = "bbbbbbbb-0000-4000-8000-000000000002"
OTHER_ENV_ID = "aaaaaaaa-0000-4000-8000-000000000001"
JIRA_KEY = "DSO-100"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_event(
    ticket_dir: Path,
    event_type: str,
    data: dict[str, Any],
    *,
    env_id: str = OTHER_ENV_ID,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a well-formed event JSON file and return its path."""
    ts = int(time.time())
    event_uuid = str(uuid.uuid4())
    filename = f"{ts}-{event_uuid}-{event_type}.json"
    payload: dict[str, Any] = {
        "event_type": event_type,
        "timestamp": ts,
        "uuid": event_uuid,
        "env_id": env_id,
        "data": data,
    }
    if extra:
        payload.update(extra)
    path = ticket_dir / filename
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def write_sync(ticket_dir: Path, jira_key: str) -> Path:
    """Write a SYNC event to establish the Jira link."""
    ts = int(time.time())
    event_uuid = str(uuid.uuid4())
    filename = f"{ts}-{event_uuid}-SYNC.json"
    payload = {
        "event_type": "SYNC",
        "jira_key": jira_key,
        "local_id": ticket_dir.name,
        "env_id": BRIDGE_ENV_ID,
        "timestamp": ts,
        "run_id": "test-run",
    }
    path = ticket_dir / filename
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def make_create_event(
    ticket_dir: Path,
    *,
    title: str = "Test Ticket",
    ticket_type: str = "bug",
    priority: int = 2,
    assignee: str = "testuser",
    description: str = "A test description",
    env_id: str = OTHER_ENV_ID,
) -> Path:
    """Write a CREATE event with all fields populated."""
    ts = int(time.time())
    event_uuid = str(uuid.uuid4())
    filename = f"{ts}-{event_uuid}-CREATE.json"
    payload = {
        "event_type": "CREATE",
        "timestamp": ts,
        "uuid": event_uuid,
        "env_id": env_id,
        "author": "Test User",
        "data": {
            "ticket_type": ticket_type,
            "title": title,
            "priority": priority,
            "assignee": assignee,
            "description": description,
        },
    }
    path = ticket_dir / filename
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path
