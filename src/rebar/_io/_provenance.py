"""Translate exported records into fresh local-create kwargs.

The locked writer assigns local IDs and HLCs; source identity becomes ``source_*``
metadata. This module alone owns that provenance mapping.
"""

from __future__ import annotations

from typing import Any


def _coerce_ns(value: Any) -> Any:
    """Return exact nanoseconds as ``int`` from JSON numbers or decimal strings.

    Preserve ``None`` and malformed provenance so one field cannot abort the import row.
    """
    if value is None or isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def create_kwargs(record: dict[str, Any]) -> dict[str, Any]:
    """Build ``create_ticket`` kwargs, leaving parent wiring for later.

    Derive ``source_*`` from this record, not inherited provenance. The fresh local ticket's
    genesis channel is ``import``; its prior origin remains in ``source_*``.
    """
    return {
        "ticket_type": record.get("ticket_type"),
        "title": record.get("title") or "",
        "description": record.get("description") or "",
        "priority": record.get("priority"),
        "assignee": record.get("assignee"),
        "tags": list(record.get("tags") or []),
        "_creation_channel": "import",
        "source": {
            "source_id": record.get("ticket_id"),
            "source_created_at": _coerce_ns(record.get("created_at")),
            "source_author": record.get("author"),
            "source_env": record.get("env_id"),
        },
    }


def comment_source(entry: dict[str, Any]) -> dict[str, Any]:
    """Per-comment provenance kwargs (``source_author``/``source_created_at``)."""
    return {
        "source_author": entry.get("author"),
        "source_created_at": _coerce_ns(entry.get("timestamp")),
    }
