"""Pytest configuration for unit tests.

Adds the engine directory (``src/rebar/_engine``) to ``sys.path`` so engine unit
tests can import the bundled helpers by their on-disk names without each test
file manipulating ``sys.path`` itself. After the ``fare-rant-clasp`` repackage the
old top-level names (``ticket_reducer`` / ``ticket_graph`` / ``ticket_reads`` …)
resolve here to thin compat shims that re-export the real ``rebar.*`` subpackages,
so these imports keep working while exercising the same code the library loads.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "src" / "rebar" / "_engine")

if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
