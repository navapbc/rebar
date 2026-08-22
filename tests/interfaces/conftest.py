"""Fixtures for the interface-parity tier.

These tests exercise the three rebar interfaces (Python library, CLI, MCP) over
ONE git-backed ticket store, asserting they behave identically. The tier is
intentionally outside the unit/scripts network guard (it subprocesses git, no
network) and uses a real temp git repo per test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from _store_template import (
    _clone_template,
    worktree_paths,
)

import rebar

# Make this directory importable from subdirectory tests. pytest's prepend
# import mode puts each test file's own dir on sys.path, but the seam
# subdirectories under tests/interfaces/ do not contain ``adapters.py`` — it
# lives here at the interfaces root. A parent-dir conftest is imported before
# descending into subdirs, so this insertion runs before the subdir test
# modules load, keeping ``from adapters import ...`` resolvable everywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture(scope="session")
def _rebar_repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The canonical initialized store, built ONCE and copied per test.

    Session-scoped via ``tmp_path_factory``, which under xdist roots each worker at
    its own basetemp — so this is one template per worker with no shared path. It
    must never become a fixed location (e.g. /tmp/rebar-template): workers would
    race to build and mutate one directory.

    The build runs inside its own MonkeyPatch context because a session fixture
    cannot request the function-scoped ``monkeypatch`` and materializes lazily inside
    whichever test asks first — whose autouse env is already applied, and which
    varies per worker per run under ``--dist worksteal``. Pinning here keeps a stray
    REBAR_TRACKER_DIR/BRANCH from being baked into the template all 589 tests copy.
    """
    from rebar import config as _cfg

    root = tmp_path_factory.mktemp("rebar-repo-template")
    repo = root / "template"
    repo.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REBAR_ROOT", str(repo))
        mp.setenv("XDG_CONFIG_HOME", str(root / "xdg-empty"))
        for var in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
            mp.delenv(var, raising=False)
        _cfg.reset_config_cache()
        try:
            _git("init", "-q", cwd=repo)
            _git("config", "user.email", "test@example.com", cwd=repo)
            _git("config", "user.name", "Test", cwd=repo)
            mp.chdir(repo)
            rebar.init_repo(repo_root=str(repo))
            # Give the CODE branch a root commit so the suite-wide attested/``ref=HEAD``
            # gate default (tests/conftest.py) can resolve a snapshot: an unborn HEAD
            # fails ref resolution before any gate op reaches its subject under test.
            _git("commit", "--allow-empty", "-q", "-m", "init", cwd=repo)
            # The template must be VIRGIN, asserted HERE rather than only in a test.
            # Several tests assert on emptiness (session-log counts in
            # queries/test_recent_session_logs.py and lifecycle/test_session_log_capture.py)
            # and hold only because nothing seeds the template; pre-warming it with
            # tickets — the obvious next optimisation — would silently invert them.
            # At construction this fires once per worker before any test runs and covers
            # every consumer, whereas a test-level check protects only runs that select
            # that test (a narrowed `pytest tests/interfaces/store` would have none).
            # It must sit INSIDE this context: it needs REBAR_ROOT and cwd pinned at the
            # template, and the post-build assertions below run after the context exits.
            assert rebar.list_tickets() == [], "template must be built with no tickets"
        finally:
            _cfg.reset_config_cache()

    # Prove the pinning took effect rather than assuming it.
    tracker = repo / ".tickets-tracker"
    assert tracker.is_dir(), f"template built without a tracker at {tracker}"
    branch = subprocess.run(
        ["git", "-C", str(tracker), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "tickets", f"template tracker on branch {branch!r}, expected 'tickets'"
    # The template must stay VIRGIN. Tests assert on emptiness (e.g. session-log
    # counts), and those assertions hold only because nothing is pre-seeded here.
    # Pre-warming this template with tickets would silently invert them.
    assert not (repo / ".rebar").exists(), "template must not carry .rebar state"
    return repo


@pytest.fixture
def rebar_repo(
    _rebar_repo_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """An initialized rebar repo in a temp git dir.

    Sets REBAR_ROOT and cwd to the repo so every interface resolves both the
    ticket store and project-scoped configuration from the same isolated
    checkout. This also keeps the no-repo-root-leak guard from firing on
    bridge_state/.tickets-tracker writes. Yields the repo path.

    The store is COPIED from a per-worker template rather than built (~22 ms vs
    ~306 ms); ``_clone_template`` re-points it at itself and re-mints its identity.
    """
    repo = _clone_template(_rebar_repo_template, tmp_path / "repo")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    # The gate snapshot store is keyed by COMMIT SHA ALONE (_snapshot/repo_snapshot.py),
    # and tests/conftest.py forces attested/ref=HEAD for every test. Every copy shares
    # the template's code SHA, so without a per-test store the ~150 gate-touching tests
    # here would share one snapshot entry — making "did the gate materialize MY repo?"
    # unfalsifiable, and letting the LRU janitor evict it mid-session.
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "gate"))
    monkeypatch.chdir(repo)
    # Path-keyed today, so copying is safe — but this module global is never reset by
    # the autouse config-cache fixture, and the template scheme is what would turn a
    # future re-keying into a worker-wide leak across every test at once.
    from rebar._store import ensures as _ensures

    _ensures._reset_pending_cache()
    return repo


@pytest.fixture(autouse=True)
def _store_stays_test_local(tmp_path: Path) -> Iterator[None]:
    """Tier-wide: no test may finish with a store reaching outside its own tmp_path.

    Deliberately autouse and tier-wide rather than folded into ``rebar_repo``. About
    twenty module-local fixtures in this tier build stores their own way, and a
    contributor who later "optimises" one of them by copying the template pattern
    without the pointer rewrite would reintroduce exactly the silent shared-object-store
    bug the template scheme risks. Scoping the guard to the tier means the bug cannot
    recur in a fixture that does not exist yet.

    The invariant is TEST-LOCALITY, not self-containment. Several tests legitimately
    build NESTED stores — e.g. a store at ``repo/sub`` whose worktree list names the
    enclosing ``repo`` (test_e4_init.py, test_ensures.py, test_review_regressions.py
    exercise exactly that for symlink/sub-root behavior). Those are correct and stay
    inside the test's own tmp_path. What must never happen is a store reaching into
    ANOTHER test's directory or into the shared per-worker template — which is the
    signature of a copy that was not re-pointed at itself.

    Costs one ``git worktree list`` per test, and only for tests that have a store.
    """
    yield
    root = os.environ.get("REBAR_ROOT")
    if not root:
        return
    repo = Path(root)
    if not ((repo / ".tickets-tracker").exists() and (repo / ".git").exists()):
        return
    sandbox, root = tmp_path.resolve(), repo.resolve()
    # A store passes if its worktrees are test-local OR it is genuinely self-contained
    # (the per-worker template is the latter: outside any one test's tmp_path, but it
    # references only itself). A copy that was never re-pointed satisfies NEITHER,
    # because it names a path in a third location.
    stray = [
        p
        for p in worktree_paths(repo)
        if sandbox not in (p.resolve(), *p.resolve().parents)
        and root not in (p.resolve(), *p.resolve().parents)
    ]
    if stray:
        raise AssertionError(
            f"store at {repo.resolve()} references worktrees outside this test's "
            f"sandbox {sandbox}: {stray}. A copied store was not re-pointed at itself "
            "and is sharing another store's object database and refs."
        )
