"""Shared helper: locate the bundled rebar engine dir from the tests tree.

All conftests import this so there is one place to adjust if the layout moves.
The engine lives at ``<repo>/src/rebar/_engine`` and the tests tree at
``<repo>/tests/...``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def repo_root() -> Path:
    # tests/_engine_path.py -> tests -> <repo>
    return Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def engine_dir() -> Path:
    return repo_root() / "src" / "rebar" / "_engine"


def acli_path() -> Path:
    return engine_dir() / "acli-integration.py"
