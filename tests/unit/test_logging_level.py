"""Log-level resolution precedence.

Moved from the retired ``tests/scripts/test_residual_drain_seam_clean_heldout.py`` guard
(story skaldic-paradoxal-huemul); the behaviour test is unchanged.
"""

from __future__ import annotations

import pytest


def test_log_level_resolution_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """``REBAR_LOG_LEVEL`` still drives the handler level: a symbolic name, a numeric
    level, and the WARNING default all resolve as before the disposal."""
    import logging

    from rebar._logging import _resolve_level

    monkeypatch.delenv("REBAR_LOG_LEVEL", raising=False)
    assert _resolve_level() == logging.WARNING, "unset REBAR_LOG_LEVEL must default to WARNING"
    monkeypatch.setenv("REBAR_LOG_LEVEL", "DEBUG")
    assert _resolve_level() == logging.DEBUG, "a symbolic level name must resolve"
    monkeypatch.setenv("REBAR_LOG_LEVEL", "10")
    assert _resolve_level() == 10, "a numeric level must resolve"
