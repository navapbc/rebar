"""Shared fixtures for the reducer test split (tests/scripts/reducer/).

Composes with the parent tests/scripts/conftest.py (sys.path + network guard)
and tests/conftest.py (repo-isolation guard). Holds the module-scoped `reducer`
fixture that loads the hyphenated ticket-reducer.py once per split file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# Make sibling helper modules (_events) importable regardless of import mode.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# reducer/conftest.py -> reducer -> scripts -> tests -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket-reducer.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ticket_reducer", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def reducer() -> ModuleType:
    """Return the ticket-reducer module, failing all tests if absent."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"ticket-reducer.py not found at {SCRIPT_PATH} — "
            "implement the script to make tests pass."
        )
    return _load_module()
