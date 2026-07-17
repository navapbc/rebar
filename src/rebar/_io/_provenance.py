"""Map an export NDJSON record → write-path kwargs, carrying provenance (P1.2).

An imported ticket gets a FRESH local id and FRESH HLC timestamps (the locked
write path assigns them); the source store's identity is preserved as ``source_*``
metadata on the CREATE/COMMENT events (T1 plumbing), never injected as foreign
timestamps. This module is the single place that decides which export fields become
provenance, so the importer stays declarative.
"""

from __future__ import annotations

from typing import Any


def create_kwargs(record: dict[str, Any]) -> dict[str, Any]:
    """Keyword args for ``create_ticket`` from one export record (parent set later).

    Provenance: the record's own ``ticket_id``/``created_at``/``author``/``env_id``
    become ``source_*`` — i.e. provenance points at the store we are importing FROM,
    not at any earlier ancestor a re-exported record might also carry.

    ``_creation_channel`` (story e622) is pinned to ``"import"``: the NDJSON importer
    creates a FRESH LOCAL ticket, so its own genesis channel is the import ingress —
    NOT a copy of whatever channel the exported source record carried (the source's
    origin lives on in the ``source_*`` provenance instead).
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
            "source_created_at": record.get("created_at"),
            "source_author": record.get("author"),
            "source_env": record.get("env_id"),
        },
    }


def comment_source(entry: dict[str, Any]) -> dict[str, Any]:
    """Per-comment provenance kwargs (``source_author``/``source_created_at``)."""
    return {
        "source_author": entry.get("author"),
        "source_created_at": entry.get("timestamp"),
    }
