"""Loader for ticket-reducer module (importlib, handles hyphenated filename)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REDUCER_PATH = _SCRIPT_DIR / "ticket-reducer.py"


def _load_reducer() -> Any:
    spec = importlib.util.spec_from_file_location("ticket_reducer", _REDUCER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load ticket-reducer.py from {_REDUCER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# Module-level singleton — loaded once per process
reducer = _load_reducer()
reduce_ticket = reducer.reduce_ticket
reduce_all_tickets = reducer.reduce_all_tickets
