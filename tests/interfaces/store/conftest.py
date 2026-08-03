"""Fixtures for the store interface tier.

`repo_with_origin_tickets` moved here from ``test_show_no_stall.py`` (ticket fa6e)
so the generic reads-under-sync-contention property module and the ed2b regression
share it. Helper functions live in ``sync_contention_harness.py`` — import from
there, not from a test module (the sys.path insert below mirrors
``tests/scripts/conftest.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from sync_contention_harness import build_repo_with_origin_tickets  # noqa: E402


@pytest.fixture
def repo_with_origin_tickets(tmp_path, monkeypatch):
    """A repo whose tracker has an `origin/tickets` upstream, so `ensure_fresh`
    actually reconverges (it early-returns when there's no remote branch). Yields
    (repo_path, tracker_path, ticket_id)."""
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    repo, tracker, tid = build_repo_with_origin_tickets(tmp_path)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    return repo, tracker, tid
