"""Repo-wide pytest configuration.

Provides an autouse fixture that prevents tests from creating new top-level
entries in REPO_ROOT. Tests that write to disk must use ``tmp_path`` or
another sandboxed location. If a test leaks, the leak is cleaned up and the
test fails with a message naming the new entries.

This guard catches the most common leak shape — relative-path writes from
mis-routed tracker_dir/cwd handling (the failure mode that put
``depends_on/tkt-src3`` at the repo root). It does NOT catch writes that
target an existing top-level dir (e.g. ``src/rebar/_engine/x.json``); for
that level of guarantee, run ``git status --porcelain`` in CI.

Also provides a network-escape guard for tests/unit/** and tests/scripts/**.
Any test in those tiers that opens a real socket raises ``RuntimeError`` with a
clear message. Tests that legitimately need network access (none expected in
these tiers) may opt out via ``@pytest.mark.allow_network``.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Make tests/ importable so this conftest (and tests) can use the shared helpers
# next to it (_isolation, _engine_path) regardless of pytest's import mode.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so pytest does not emit UnknownMarkWarning."""
    config.addinivalue_line(
        "markers",
        "allow_network: opt out of the network-escape guard for tests that "
        "legitimately require real network access (use sparingly; not expected "
        "in unit or scripts tiers).",
    )
    config.addinivalue_line(
        "markers",
        "unit: mark a test as a unit test.",
    )
    config.addinivalue_line(
        "markers",
        "allow_repo_writes: opt out of the repo-isolation guard for a test that "
        "legitimately commits to or mutates this checkout (none expected — tests "
        "operate on disposable trackers under tmp_path).",
    )


_EXTERNAL_DIR = _REPO_ROOT / "tests" / "external"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Confine the ``external`` tier to tests/external/ (bug 4a48-6dd5-aef3-4c8e).

    Two structural guarantees:

    (a) Auto-apply the ``external`` marker to every collected item whose file
        lives under tests/external/, so existing tests need no per-file edits and
        the default selection ``-m "not integration and not external"`` reliably
        excludes the whole tier by directory.

    (b) Hard-FAIL collection if any item carries the ``external`` marker but is
        NOT under tests/external/ — a live/billable test must never hide in
        another tier. This is the one unambiguous confinement rule; it does not
        require a tier marker on the many existing non-external tests.
    """
    misplaced: list[str] = []
    for item in items:
        try:
            test_path = Path(item.fspath).resolve()
        except Exception:
            continue
        under_external = test_path.is_relative_to(_EXTERNAL_DIR)
        if under_external:
            item.add_marker("external")
        elif item.get_closest_marker("external") is not None:
            misplaced.append(f"{item.nodeid} ({test_path})")

    if misplaced:
        listing = "\n  ".join(misplaced)
        pytest.fail(
            "External-test confinement violation: the following item(s) are "
            "marked `external` but live OUTSIDE tests/external/. Live/billable "
            "external tests must reside under tests/external/ so the env opt-in "
            "and credential-scoped CI job confine them:\n  " + listing,
            pytrace=False,
        )


# Directories whose tests are network-isolated by the socket guard.
_NETWORK_GUARDED_TIERS = (
    _REPO_ROOT / "tests" / "unit",
    _REPO_ROOT / "tests" / "scripts",
)


def _in_guarded_tier(item: pytest.Item) -> bool:
    """Return True if *item* lives under one of the network-guarded test dirs."""
    try:
        test_path = Path(item.fspath).resolve()
    except Exception:
        return False
    return any(test_path.is_relative_to(tier) for tier in _NETWORK_GUARDED_TIERS)


def _guard_socket_connect(*args: object, **kwargs: object) -> None:
    raise RuntimeError(
        "Network access is forbidden in unit/scripts tests. "
        "Mock the network call (e.g. unittest.mock.patch('urllib.request.urlopen')) "
        "or add @pytest.mark.allow_network if this test genuinely needs network access."
    )


@pytest.fixture(autouse=True)
def _network_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Block real socket connections in unit and scripts test tiers.

    Patches ``socket.socket.connect`` and ``socket.create_connection`` to raise
    ``RuntimeError`` for every test whose path falls under tests/unit/ or
    tests/scripts/, unless the test is decorated with
    ``@pytest.mark.allow_network``.

    Uses stdlib ``unittest.mock.patch`` — no new dependencies.
    """
    if not _in_guarded_tier(request.node):
        yield
        return
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    with (
        patch.object(socket.socket, "connect", _guard_socket_connect),
        patch(
            "socket.create_connection",
            side_effect=RuntimeError(
                "Network access is forbidden in unit/scripts tests. "
                "Mock the network call or add @pytest.mark.allow_network."
            ),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_repo_root_leaks() -> Iterator[None]:
    before = set(os.listdir(_REPO_ROOT))
    try:
        yield
    finally:
        after = set(os.listdir(_REPO_ROOT))
        leaked = after - before
        if leaked:
            for name in leaked:
                target = _REPO_ROOT / name
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    try:
                        target.unlink()
                    except OSError:
                        # Cleanup is best-effort — pytest.fail() below already
                        # surfaces the leak. Suppressing keeps a permissions or
                        # races race from masking the real failure.
                        pass
            pytest.fail(
                "Test leaked new entries into REPO_ROOT (use tmp_path or a "
                f"sandboxed temp dir): {sorted(leaked)}"
            )


# ── Repo-isolation guard (no test may commit to / mutate this checkout) ───────
#
# Tests operate on disposable trackers under tmp_path, never the rebar checkout.
# Two ways a test can break that, both invisible to the top-level leak guard
# above:
#   1. Commits — a write path (e.g. ticket-graph's _write_link_event running
#      ``git -C <tracker> commit``) against a tracker that is NOT its own git
#      repo: git walks UP and commits into this checkout. This once leaked dozens
#      of ``ticket: link ...`` commits onto main.
#   2. Working-tree writes into EXISTING tracked dirs (e.g. src/rebar/_engine/x),
#      which `_no_repo_root_leaks` (new top-level entries only) cannot see.
#
# The per-test fixture catches (1) and pinpoints the offender; the session
# backstop catches (2) anywhere in the tree. Both are cheap (a couple of `git`
# calls). Opt a deliberate exception out with ``@pytest.mark.allow_repo_writes``.
# Detection primitives live in tests/_isolation.py so the guard's self-test can
# exercise the same code (tests/unit/test_repo_isolation_guard.py).

from _isolation import head as _repo_head  # noqa: E402
from _isolation import porcelain as _repo_porcelain  # noqa: E402


@pytest.fixture(autouse=True)
def _no_repo_commits(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail any test that moves this checkout's HEAD (i.e. commits into it)."""
    if request.node.get_closest_marker("allow_repo_writes"):
        yield
        return
    before = _repo_head(_REPO_ROOT)
    yield
    if before is None:
        return
    after = _repo_head(_REPO_ROOT)
    if after is not None and after != before:
        pytest.fail(
            f"Test moved the repo HEAD ({before[:10]} -> {after[:10]}): it "
            "committed into the rebar checkout instead of an isolated tmp "
            "tracker. Isolate the git writes — pin GIT_CEILING_DIRECTORIES to the "
            "tmp root (see tests/scripts/graph/conftest.py::"
            "_isolate_git_from_enclosing_repo) or init the tracker as its own "
            f"git repo. Undo the stray commit(s) with: git reset --hard {before[:10]}"
        )


# Session-level working-tree backstop: snapshot `git status --porcelain` at the
# start and compare at the end, failing the run if any NEW dirty entry appeared.
# Compares net-new (not absolute) so a developer's pre-existing uncommitted work
# never trips it. gitignored paths (e.g. .pytest-tmp/, __pycache__/) are excluded
# by porcelain, so normal runs stay clean.
_PORCELAIN_AT_START: set[str] | None = None


def pytest_sessionstart(session: pytest.Session) -> None:
    global _PORCELAIN_AT_START
    _PORCELAIN_AT_START = _repo_porcelain(_REPO_ROOT)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _PORCELAIN_AT_START is None:
        return
    after = _repo_porcelain(_REPO_ROOT)
    if after is None:
        return
    leaked = sorted(after - _PORCELAIN_AT_START)
    if not leaked:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    msg = (
        "REPO ISOLATION FAILURE: the test run left new changes in the checkout "
        "(a test wrote into the working tree instead of tmp_path). Offending "
        "entries from `git status --porcelain`:\n  " + "\n  ".join(leaked[:40])
    )
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line(msg, red=True, bold=True)
    else:  # pragma: no cover - terminalreporter always present under pytest
        print(msg)
    # Escalate the run to a failure so CI catches it even if every test "passed".
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
