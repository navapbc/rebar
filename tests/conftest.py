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
from typing import Any
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
        except (AttributeError, OSError, ValueError):
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
    except (AttributeError, OSError, ValueError):
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


def _is_coverage_artifact(name: str) -> bool:
    """coverage.py's own data files are NOT test leaks.

    Under pytest-xdist (`-n>0`) with ``parallel = true`` (see docs/coverage.md), each
    worker process writes a per-process data file — ``.coverage.<host>.<pid>.<rand>`` —
    to the CWD (repo root) when it finishes, and pytest-cov combines them into a single
    ``.coverage`` at session end. Because workers finish at different times, a file
    written by a done worker would otherwise be observed as a "new entry" by the
    per-test leak snapshot of a still-running worker and DELETED — corrupting the
    combine (coverage collapses) and spuriously failing that test (story 8d36). These
    names are all gitignored and produced by the coverage plugin, not the test body, so
    the leak guard skips them (it never deletes and never fails on them).
    """
    return name == ".coverage" or name.startswith(".coverage.") or name == "coverage.xml"


@pytest.fixture(autouse=True)
def _no_repo_root_leaks() -> Iterator[None]:
    from _isolation import repo_leak_snapshot as _repo_leak_snapshot

    before = _repo_leak_snapshot(_REPO_ROOT)
    try:
        yield
    finally:
        after = _repo_leak_snapshot(_REPO_ROOT)
        leaked = {name for name in (after - before) if not _is_coverage_artifact(name)}
        if leaked:
            # Deepest-first so a leaked file under a watched dir is removed before
            # we would touch the dir itself (top-level names sort shorter).
            for name in sorted(leaked, key=len, reverse=True):
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


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Portability/isolation: config is now resolved on the read path via
    ``rebar.config.load_config``, which reads a user-level config
    (``$XDG_CONFIG_HOME/rebar/config.toml``). Point XDG at an empty per-test dir so
    no test ever reads the developer's real ``~/.config/rebar/config.toml`` (host
    leakage would make results machine-dependent), and drop any ambient
    ``REBAR_CONFIG`` pointer. Tests that need a user config set ``XDG_CONFIG_HOME``
    themselves; this only removes host leakage."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    monkeypatch.delenv("REBAR_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def _gate_source_local_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the code-reading gates to ``source=local`` for the offline test suite
    (epic raze-vet-ditch).

    In production the gates default to ``source=attested``/``ref=origin/main`` — they
    fetch + materialize a snapshot at the pinned SHA. The test suite runs OFFLINE on
    disposable ``tmp_path`` repos that have no ``origin`` remote, so attested would
    (correctly) fail closed resolving ``origin/main``. ``local`` reads the in-place
    checkout — the faithful continuation of the pre-snapshot behavior these gate-logic
    tests assert. A test that specifically exercises the attested path sets
    ``REBAR_GATE_SOURCE`` / passes ``source="attested"`` explicitly (an explicit arg
    wins over this default), so the attested path is still covered."""
    if "REBAR_GATE_SOURCE" not in os.environ:
        monkeypatch.setenv("REBAR_GATE_SOURCE", "local")


@pytest.fixture(autouse=True)
def _identity_enforcement_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the authenticated-authorship write-gate OFF for the suite (story ad42).

    A stray global ``REBAR_IDENTITY_REQUIRE_AUTHENTICATED=1`` in the environment would
    otherwise break the suite broadly: every create/mutate of a non-exempt ticket type
    fails with the "cannot be signed" CommandError when no identity + signing key is
    configured. This guard pins the enforcement flag to ``0`` for the in-process suite so
    results never depend on an ambient global. Unlike the gate-source default above this is
    UNCONDITIONAL (it must override a stray ``=1``, not defer to it). The dedicated identity
    enforcement tests (``tests/unit/test_identity_*``) are unaffected: they build their own
    subprocess ``env`` dict with the flag set explicitly and pass ``env=`` to
    ``subprocess.run``, so their value wins in the child process regardless of this default."""
    monkeypatch.setenv("REBAR_IDENTITY_REQUIRE_AUTHENTICATED", "0")


@pytest.fixture(autouse=True)
def _compaction_horizon_zero_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the compaction horizon to 0 for the offline suite (RC2b, 36d1).

    In production ``compact.COMPACTION_HORIZON_NS`` defaults to 1800 s so recent
    "hot-edge" events are not folded (they may still gain a concurrent sub-horizon
    sibling on another clone). The test suite creates events and compacts them
    milliseconds later — with the production default every fresh event is "young" and
    nothing would ever fold, breaking every compaction test. Horizon 0 makes the
    pre-RC2b behavior the test baseline. A test that specifically exercises the
    horizon sets ``REBAR_COMPACTION_HORIZON_NS`` (or a config file) itself — an
    explicit value wins over this default."""
    if "REBAR_COMPACTION_HORIZON_NS" not in os.environ:
        monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")


@pytest.fixture(autouse=True)
def _reset_config_cache() -> None:
    """``load_config`` memoizes resolution per process (perf: it is on the command
    hot path). Tests reconfigure env/files freely between cases, so clear the caches
    around each test — no resolved Config or parsed-TOML entry leaks across tests."""
    from rebar import config as _cfg

    _cfg.reset_config_cache()
    yield
    _cfg.reset_config_cache()


@pytest.fixture(scope="session", autouse=True)
def _no_live_model_requests() -> None:
    """CI safety net: forbid accidental live LLM calls from the test suite.

    pydantic-ai exposes a global kill-switch (``models.ALLOW_MODEL_REQUESTS``); set it
    False so any code path that reaches a real model request raises instead of billing.
    Guarded — ``pydantic_ai`` is behind the ``[agents]`` extra and is absent in the
    lean-install lanes, where this is simply a no-op.
    """
    try:
        from pydantic_ai import models as _pai_models
    except Exception:  # noqa: BLE001 — agents extra absent (lean lane): nothing to guard
        return
    _pai_models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture
def block_extra() -> Iterator[Any]:
    """Simulate an UNINSTALLED module/extra by blocking its import via ``sys.meta_path``.

    Yields a ``block(*module_names)`` callable. The inserted finder raises
    ``ModuleNotFoundError`` for the named modules (and their submodules), so
    ``importlib.util.find_spec`` / ``import`` see them as absent — exercising the
    optional-dependency degradation path in-process (precedent: kopf / linkml).

    Opt-in (NOT autouse). Restores ``sys.meta_path`` + any evicted ``sys.modules``
    entries and invalidates the import caches on teardown, so the global import state
    never leaks across tests (incl. under pytest-xdist).
    """
    import importlib

    inserted: list[Any] = []
    saved_modules: dict[str, Any] = {}
    blocked: set[str] = set()

    class _Blocker:
        def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
            if fullname in blocked or any(fullname.startswith(b + ".") for b in blocked):
                raise ModuleNotFoundError(f"{fullname} blocked by the block_extra fixture")
            return None

    def _block(*names: str) -> None:
        for name in names:
            blocked.add(name)
            # Evict any already-imported copy (+ submodules) so find_spec is consulted.
            for mod in list(sys.modules):
                if mod == name or mod.startswith(name + "."):
                    saved_modules.setdefault(mod, sys.modules[mod])
                    del sys.modules[mod]
        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)
        inserted.append(blocker)
        importlib.invalidate_caches()

    yield _block

    for blocker in inserted:
        try:
            sys.meta_path.remove(blocker)
        except ValueError:  # pragma: no cover — defensive
            pass
    sys.modules.update(saved_modules)
    importlib.invalidate_caches()


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
