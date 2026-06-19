"""Shared fixtures for the ticket-graph test split (tests/scripts/graph/).

Composes with the parent tests/scripts/conftest.py (sys.path + network guard) and
tests/conftest.py (repo-isolation guard). Holds the module-scoped `graph` fixture
and the autouse git-isolation fixture every writing test relies on.
"""

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
    """Make every test in this module fully hermetic with respect to git.

    ``add_dependency()`` -> ``_write_link_event()`` runs
    ``git -C <tracker> add/commit`` (and a best-effort push). The trackers built
    here are plain directories, not git repos, so without a boundary git walks
    UP and can commit the test's LINK events into whatever repo encloses the
    pytest tmp dir — e.g. the rebar checkout itself when the pytest basetemp is
    nested inside it (the failure mode that once leaked ``ticket: link ...``
    commits onto main).

    Pin ``GIT_CEILING_DIRECTORIES`` so git can never chdir up out of the
    disposable tmp tree — nor into the rebar checkout — while searching for a
    repository. With no enclosing repo reachable, ``git add`` against a non-repo
    tracker fails cleanly (``_write_link_event`` already swallows that) and the
    LINK event file — which is all ``build_dep_graph`` reads — is still written.
    Tests that exercise a real push create their own repo + remote *under*
    ``tmp_path`` (below the ceiling), so they are unaffected.
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
    yield


@pytest.fixture(autouse=True)
def _link_writes_persist_without_repo(monkeypatch):
    """Persist canonical LINK events on bare-directory trackers (no git repo).

    This suite tests ``add_dependency`` as a *pure graph operation* — cycle and
    hierarchy rules, idempotency, and the LINK event file that ``build_dep_graph``
    reads — against plain-directory trackers (see ``_isolate_git_from_enclosing_repo``).
    Production routes LINK writes through the ONE canonical store path
    (``rebar._store.event_append.write_and_push``), which legitimately refuses to
    write to an un-initialized store (no ``.git``). Rather than stand up a full git
    store per test, persist the *composed* event file directly when the tracker is
    not a repo (the durability/commit step is out of scope here and is covered by
    the store-layer tests), and delegate to the real committer when a repo IS present
    (the push-policy e2e tests create their own repo+remote under tmp_path).
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
    yield
