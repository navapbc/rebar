"""Canonical JSON Schemas for rebar's machine-readable outputs.

These schema files are the single source of truth for the shape of rebar's JSON
outputs (e.g. the compiled ticket state from ``rebar show``). They are used to:

  * document the output contract,
  * validate real output across the CLI / library / MCP interfaces in tests, and
  * advertise output schemas to MCP clients (see ``rebar.mcp_server``).

Schemas are stdlib-only package data (no runtime dependency); load them with
:func:`load`.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

__all__ = ["load", "path", "TICKET_STATE"]

TICKET_STATE = "ticket_state"


def path(name: str) -> Path:
    """Filesystem path to the ``<name>.schema.json`` file (packaged data)."""
    return Path(str(files(__package__).joinpath(f"{name}.schema.json")))


def load(name: str) -> dict[str, Any]:
    """Parse and return the ``<name>.schema.json`` schema as a dict."""
    return json.loads(path(name).read_text(encoding="utf-8"))
