"""Pytest configuration for unit tests.

Adds the figma_merge package directory (src/rebar/_engine) to sys.path so
that ``from figma_merge.<submodule> import ...`` works for all unit tests in
this directory without each test file needing to manipulate sys.path itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "src" / "rebar" / "_engine")

if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
