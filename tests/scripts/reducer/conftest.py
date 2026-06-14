"""Shared fixtures for the reducer test split (tests/scripts/reducer/).

Composes with the parent tests/scripts/conftest.py (sys.path + network guard)
and tests/conftest.py (repo-isolation guard). Holds the module-scoped `reducer`
fixture that loads the hyphenated ticket-reducer.py once per split file.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

# Make sibling helper modules (_events) importable regardless of import mode.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@pytest.fixture(scope="module")
def reducer() -> ModuleType:
    """Return the canonical reducer package (Tier E E7: was the hyphenated
    ``ticket-reducer.py`` CLI wrapper, which only re-exported ``rebar.reducer``)."""
    import rebar.reducer

    return rebar.reducer
