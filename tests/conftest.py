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
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar
from unittest.mock import patch

import pytest

_CallResult = TypeVar("_CallResult")

_SLOW_SPAN_THRESHOLD_SECONDS = 1.0
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _fixture_timing_enabled() -> bool:
    """Return whether thresholded pytest timing should be emitted for this process."""
    explicit = os.environ.get("REBAR_FIXTURE_TIMING", "").strip().lower()
    ci = os.environ.get("CI", "").strip().lower()
    return explicit == "1" or ci in _TRUTHY_ENV_VALUES


def _report_slow_ci_span(
    node_id: str,
    fixture_name: str,
    span_name: str,
    elapsed_seconds: float,
) -> None:
    """Publish a slow setup span through pytest's xdist-safe warning channel."""
    if not _fixture_timing_enabled() or elapsed_seconds <= _SLOW_SPAN_THRESHOLD_SECONDS:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    warnings.warn(
        pytest.PytestWarning(
            f"[rebar-ci-timing] worker={worker} node={node_id} "
            f"fixture={fixture_name} span={span_name} elapsed={elapsed_seconds:.3f}s"
        ),
        stacklevel=2,
    )


def _timed_ci_call(
    node_id: str,
    fixture_name: str,
    span_name: str,
    call: Callable[..., _CallResult],
    *args: Any,
    **kwargs: Any,
) -> _CallResult:
    """Call transparently and report only spans above the CI timing threshold."""
    started = time.monotonic()
    try:
        return call(*args, **kwargs)
    finally:
        _report_slow_ci_span(
            node_id,
            fixture_name,
            span_name,
            time.monotonic() - started,
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
) -> Iterator[None]:
    """Attribute slow fixture setup to its worker, node, and fixture name."""
    started = time.monotonic()
    yield
    _report_slow_ci_span(
        request.node.nodeid,
        fixturedef.argname,
        "fixture_setup",
        time.monotonic() - started,
    )


@pytest.fixture
def _ci_timed_call(request: pytest.FixtureRequest) -> Callable[..., Any]:
    """Bind nested-span timing to the current test node for targeted fixtures."""

    def timed_call(
        fixture_name: str,
        span_name: str,
        call: Callable[..., _CallResult],
        *args: Any,
        **kwargs: Any,
    ) -> _CallResult:
        return _timed_ci_call(
            request.node.nodeid,
            fixture_name,
            span_name,
            call,
            *args,
            **kwargs,
        )

    return timed_call


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Make tests/ importable so this conftest (and tests) can use the shared helpers
# next to it (_isolation, _engine_path) regardless of pytest's import mode.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


# --- Optional-extra coverage guard (bug 599e-77da-29dd-482d) ----------------------
# A whole test surface can silently vanish from CI: with no lane installing the `reviewbot`
# extra, 38 tests behind `pytest.importorskip("fastapi")` — including SECURITY guards —
# no-op'd for months. With REBAR_REQUIRE_EXTRAS=1 (set by the CI pytest steps) those skips
# become hard errors instead. Lives in tests/_extra_guard.py so it is directly testable;
# imported here because a conftest is the earliest hook that runs before any test module.
import _extra_guard  # noqa: E402  (needs _TESTS_DIR on sys.path, set just above)

_extra_guard.install()


def pytest_configure(config: pytest.Config) -> None:
    """Enforce the declared Git floor, then register custom markers.

    The floor is checked FIRST and fails the whole session (it never skips): the
    two-clone convergence regressions need ``git merge-tree --write-tree`` (Git 2.38+),
    and a regression that quietly does not run reads as coverage while providing none —
    ticket 980d-83ac-a6bb-4edb. The declared value lives in
    ``.github/git-version-floor.txt``, shared with the contributor docs and the CI gate.
    """
    import _git_floor

    violation = _git_floor.floor_violation()
    if violation is not None:
        raise pytest.UsageError(violation)

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
    config.addinivalue_line(
        "markers",
        "real_reconcile_loop: run the review-bot app's real reconcile loop instead "
        "of the module's test-safe default stub.",
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
def _remove_readonly_run_snapshots(tmp_path: Path, _isolate_user_config: None) -> Iterator[None]:
    """Remove read-only workflow snapshots created below a test's temp root."""
    yield

    from rebar.llm.workflow import snapshot

    snapshot_roots = tuple(tmp_path.rglob(".rebar/run_snapshots"))
    for snapshot_root in snapshot_roots:
        if snapshot_root.is_dir():
            snapshot._rmtree_writable(snapshot_root)


@pytest.fixture(autouse=True)
def _bound_review_bot_shutdown_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests from inheriting the review bot's 20-minute production drain."""
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "1.0")


@pytest.fixture(scope="session")
def _rebar_root_sandbox_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One session-scoped, store-less git repo backing the suite-wide ``REBAR_ROOT``
    default below. It has no ``tickets`` branch and is not a linked worktree, so the
    CLI's central store mount (bug ad9f) has nothing to attach and silently no-ops."""
    root = tmp_path_factory.mktemp("rebar-root-sandbox")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.fixture(autouse=True)
def _default_rebar_root_to_sandbox(
    _rebar_root_sandbox_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ``REBAR_ROOT`` to a sandbox repo for every test (bug dd62 fallout).

    Without this, any test that drives the CLI/library WITHOUT setting ``REBAR_ROOT``
    resolves the repo root to the developer/CI CHECKOUT (git toplevel of the cwd) — and a
    real, mount-eligible command then auto-attaches ``.tickets-tracker`` into the real
    repo root (bug ad9f's central mount). Under xdist that single creation lands inside
    the per-test snapshot window of EVERY concurrently running test, so the repo-root
    leak guard flags a random spray of victims — the CI failure mode this closes.

    Precedence is preserved: a test-owned fixture (``rebar_repo`` et al.) monkeypatches
    ``REBAR_ROOT`` AFTER this autouse default and wins; a test that ``delenv``s it still
    gets the cwd fallback it asked for. Only the implicit
    tests-run-against-the-checkout default is removed."""
    monkeypatch.setenv("REBAR_ROOT", str(_rebar_root_sandbox_repo))


@pytest.fixture(autouse=True)
def _gate_source_attested_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the code-reading gates to ``source=attested``/``ref=HEAD`` for the offline
    test suite (epic raze-vet-ditch; retargeted by melancholy-firstborn-shihtzu).

    In production the gates default to ``source=attested``/``ref=origin/main`` — they fetch
    + materialize a snapshot at the pinned SHA. The suite runs OFFLINE on disposable
    ``tmp_path`` repos with no ``origin``, so ``origin/main`` cannot resolve.

    This USED to default to ``source=local`` (read the in-place checkout), which resolves
    offline but is **unsignable by contract** — ADR 0005 and
    ``_snapshot.repo_snapshot.SOURCE_LOCAL`` both document local as "dirty allowed, never
    signed". That made a defect (plan review signing a local read) load-bearing for ~47
    lifecycle tests, so the defect could not be fixed without the suite going red.

    ``ref=HEAD`` is the offline-safe attested basis — it resolves from the LOCAL object DB
    with no remote, and is the same recipe the completion close gate already uses
    (``source="attested", ref="HEAD", fetch=False``). Gate logic is now exercised against an
    immutable pinned tree, as in production.

    Consequence for test authors: **code drift must be COMMITTED to be visible.** Writing a
    file into the worktree no longer changes what the gate reads, because the gate reads the
    pinned snapshot — which is the honest meaning of drift (the attested basis moved), not an
    artifact. A test that specifically needs the in-place read sets ``REBAR_GATE_SOURCE`` or
    passes ``source="local"`` explicitly (an explicit arg wins), and must then expect NO
    signature."""
    if "REBAR_GATE_SOURCE" not in os.environ:
        monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
        monkeypatch.setenv("REBAR_GATE_REF", "HEAD")


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
def _compaction_trigger_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DETACHED compaction workers in the offline suite (story gaudy-gangrenous-basilisk).

    In production ``compact.trigger`` defaults to ``async``: a close spawns a detached worker
    that folds out of band, which is the compaction floor for stores with no CI and no cron.
    In a test suite that is actively harmful, and measurably so — with it on, detached children
    outlive the tests that spawned them and race the NEXT test's writes against the same temp
    store, producing "git commit failed" / "git operation failed" errors scattered across
    unrelated interface tests. The child is doing legitimate work; it is simply not this
    process's work, and a unit suite must not have background writers.

    Same shape as the network and repo-commit guards above: the ambient default is the safe
    one, and a test that specifically exercises the trigger sets ``REBAR_COMPACT_TRIGGER``
    itself (``always`` to fold inline and assert on it, or ``async`` with the spawn stubbed) —
    an explicit value wins over this default."""
    if "REBAR_COMPACT_TRIGGER" not in os.environ:
        monkeypatch.setenv("REBAR_COMPACT_TRIGGER", "off")


@pytest.fixture(autouse=True)
def _no_ambient_model_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub the nine ``REBAR_LLM_<CLASS>_<FIELD>`` model-class overrides (task 7761).

    The project-config layer is already sandboxed — ``REBAR_ROOT`` points at a scratch repo, so
    a ``[tool.rebar.llm.model_classes]`` table in rebar's OWN pyproject is never read. The ENV
    layer had no such guard, and since 7761 it matters: ``sizing.models_at_or_above`` and
    ``config.resolve_model`` now resolve model-CLASS names, so they read the class config where
    they previously read only the ``MODEL_LADDER`` constant. A developer or CI runner with
    ``REBAR_LLM_STANDARD_MODEL`` exported — exactly what an operator retargeting the standard
    class onto Bedrock has set — otherwise gets a spurious failure in
    ``test_plan_review.py::test_is_context_limit_error_and_ladder``, whose ladder assertion is
    written with literal BARE ids (MEASURED: index 1 becomes
    ``bedrock:us.anthropic.claude-sonnet-4-6``). That failure names an unrelated test and reads
    as a regression in the size ladder, so it is expensive to diagnose.

    UNCONDITIONAL, like the identity guard above rather than the horizon default: the point is to
    override a stray ambient value, not defer to it. Tests that exercise the override mechanism
    itself (``tests/unit/test_model_classes.py``) call ``monkeypatch.setenv`` in their own body,
    which runs after every fixture, so their value still wins."""
    for class_name in ("TRIVIAL", "STANDARD", "FRONTIER"):
        for field in ("MODEL", "PROVIDER", "ENDPOINT"):
            monkeypatch.delenv(f"REBAR_LLM_{class_name}_{field}", raising=False)


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
        def find_spec(self, fullname, path=None, target=None):
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


# ── caplog coverage integrity (bug 9ac2) ─────────────────────────────────────
#
# Every other gate in this repo fails closed; an unreachable log assertion fails
# OPEN. `assert not [unexpected]` is trivially true against an empty list, so a
# test whose caplog records never arrive verifies nothing and still reports
# success — indistinguishable, in the run output, from a real verification.
#
# caplog captures via a handler on the ROOT logger, so records only reach it if
# the shared parent logger both emits and propagates them. There are TWO shared
# parents — `rebar` for the library/CLI/MCP surfaces and the sibling
# `rebar_reconciler` for the reconciler subprocess's top-level-imported modules —
# and both are guarded (_log_integrity.SHARED_LOGGER_NAMES). Two process-global,
# never-restored mutations sever capture under either, and both are real:
#   * propagate = False — nothing under that root reaches the root handler
#     (rebar.review_bot.config.configure_logging did this at import time; b718).
#     pytest >= 8.4 re-attaches the capture handler to loggers that are ALREADY
#     non-propagating, so the damage is bounded to the test that flips it
#     mid-capture — but that window is silent, and the mitigation is an internal
#     detail of _pytest.logging, not a contract;
#   * setLevel(WARNING) — INFO/DEBUG records are dropped at the originating
#     logger, and caplog.at_level(INFO) without a `logger=` argument cannot undo
#     it because it raises the ROOT level (rebar._logging.install_stderr_handler
#     does this; every in-process rebar._cli.main() call leaks it onto `rebar`,
#     and every in-process rebar_reconciler.__main__.main() call leaks it onto
#     `rebar_reconciler` — bug 9151, an INFO assertion in the a4bd inbound-removal
#     suite going order-dependently red under -n 3 --dist worksteal).
#
# The two are handled differently on purpose. Disabling propagation is never
# legitimate, so it FAILS the test that did it — at the SOURCE, not at whichever
# unrelated victim happens to run next. Raising the level IS a legitimate side
# effect of exercising a real entrypoint, so it is CONTAINED: restored per-test,
# which is strictly stronger for coverage integrity than blame would be, because
# every test then starts from the same level and its log assertions hold
# regardless of run order. Detection lives in tests/_log_integrity.py so the
# guard's self-test (tests/unit/test_caplog_coverage_integrity.py) runs the same
# code.

import _log_integrity  # noqa: E402


@pytest.fixture(autouse=True)
def _rebar_log_propagation_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keep the shared rebar loggers able to reach ``caplog``, and blame whoever breaks it."""
    nodeid = request.node.nodeid
    problem = _log_integrity.propagation_failure(nodeid, phase="setup")
    if problem is not None:
        _log_integrity.restore_propagation()
        pytest.fail(problem, pytrace=False)
    baseline_levels = _log_integrity.current_level()
    try:
        yield
    finally:
        _log_integrity.restore_level(baseline_levels)
    problem = _log_integrity.propagation_failure(nodeid, phase="teardown")
    if problem is not None:
        _log_integrity.restore_propagation()
        pytest.fail(problem, pytrace=False)


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
