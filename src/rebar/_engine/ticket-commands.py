#!/usr/bin/env python3
"""CLI entry for Tier B Python leaf-write commands (docs/bash-migration.md §4).

The bash dispatcher execs this file (a real on-disk script, like ``ticket-reads.py``)
when ``REBAR_LEAF_WRITES=python`` selects the Python implementation for a ported
command. All logic lives in the importable ``rebar._commands`` package so the
library/MCP share it in-process; this shim just bootstraps the ``rebar`` package
onto ``sys.path`` (the engine dir is on PYTHONPATH for the subprocess) and forwards
argv.

Usage: ticket-commands.py <command> [args...]
"""

from __future__ import annotations

import sys
from pathlib import Path

# The engine dir is this file's parent; rebar lives two levels up (src/rebar).
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from rebar._commands import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
