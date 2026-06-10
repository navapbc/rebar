#!/usr/bin/env python3
"""CLI entry shim for the single-source read implementation (story 23d2-e0f3).

The hyphenated filename keeps it consistent with ``ticket-ready.py`` /
``ticket-search.py`` (the dispatcher execs it as a real file); all logic lives in
the importable ``ticket_reads`` module so the library/MCP can share it.

Usage: ticket-reads.py <show|list|deps|ready|search> [args...] [--no-sync]
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from ticket_reads import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
