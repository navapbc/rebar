"""Graph fixtures, with parent test-path and network guards from higher conftests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _helpers import REPO_ROOT, _load_module  # noqa: E402


@pytest.fixture(scope="module")
def graph() -> ModuleType:
    """Return the ticket-graph module, failing all tests if absent (RED)."""
    return _load_module()


@pytest.fixture(autouse=True)
def _isolate_git_from_enclosing_repo(tmp_path, monkeypatch):
    """Keep plain-directory tracker Git calls below the test's tmp root.

    LINK event files remain available to graph tests. Git cannot walk upward and commit
    them into the checkout. Push tests create their own repo below the ceiling.
    """
    import os

    ceilings = os.pathsep.join(
        # de-dupe while preserving order; cover symlinked temp roots (macOS
        # /var -> /private/var) so the ceiling matches git's resolved walk.
        dict.fromkeys(
            [
                str(tmp_path.parent),
                os.path.realpath(tmp_path.parent),
                str(REPO_ROOT),
                os.path.realpath(REPO_ROOT),
            ]
        )
    )
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", ceilings)
    return


@pytest.fixture(autouse=True)
def _link_writes_persist_without_repo(monkeypatch):
    """Persist composed LINK files directly for graph-only directory fixtures.

    A directory without Git cannot use the production committer. Git-backed push tests
    still use the production committer. Store durability is tested separately.
    """
    import os

    from rebar._store import event_append as _ea

    _real_write_and_push = _ea.write_and_push

    def _persist(tracker, ticket_id, event):
        if os.path.exists(os.path.join(str(tracker), ".git")):
            return _real_write_and_push(tracker, ticket_id, event)
        ticket_dir = os.path.join(str(tracker), ticket_id)
        os.makedirs(ticket_dir, exist_ok=True)
        fname = _ea.event_filename(event["timestamp"], event["uuid"], event["event_type"])
        with open(os.path.join(ticket_dir, fname), "wb") as fh:
            fh.write(_ea.canonical_bytes(event))
        return 0

    monkeypatch.setattr(_ea, "write_and_push", _persist)
    return
