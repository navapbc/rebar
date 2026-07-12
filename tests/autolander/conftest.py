"""pytest wiring for the auto-lander tests: put `infra/` on sys.path (so the standalone
`autolander` package imports) and expose the FakeTransport as a fixture. The fakes
themselves live in `_fakes.py` (importable as a plain module in prepend mode)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_INFRA = Path(__file__).resolve().parents[2] / "infra"
if str(_INFRA) not in sys.path:
    sys.path.insert(0, str(_INFRA))

from _autolander_fakes import FakeTransport  # noqa: E402 (after sys.path setup)


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()
