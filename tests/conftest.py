"""Configure repository-wide pytest isolation and tier selection.

Tests use sandbox paths. Report repo-root changes without deleting them because the
snapshot cannot identify their writer. Unit and script tests block unexpected sockets.
allow_network marks explicit exceptions.
"""

from __future__ import annotations

import os
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

pytest_plugins = ("_crash_guard",)

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
# next to it (_isolation, _repo_root) regardless of pytest's import mode.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


# Turn missing Python extras into collection errors when REBAR_REQUIRE_EXTRAS=1.
# Import the directly tested guard before test modules can call importorskip.
import _extra_guard  # noqa: E402  (needs _TESTS_DIR on sys.path, set just above)
from _sandbox_capabilities import (  # noqa: E402
    collect_counted_skips_from_worker,
    configure_semgrep_settings_file,
    posix_named_semaphore_probe,
    publish_counted_skips,
    record_counted_skip,
    report_counted_skips,
    scrub_ambient_git_config,
)

_extra_guard.install()


def pytest_configure(config: pytest.Config) -> None:
    """Enforce the shared Git version floor before registering markers.

    Convergence tests require merge-tree --write-tree. An older Git fails the session
    instead of silently skipping the regression. CI and docs read the same floor file.
    """
    import _git_floor

    scrub_ambient_git_config(os.environ)
    configure_semgrep_settings_file(_REPO_ROOT)
    _register_counted_skip_xdist_hooks(config)

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
    config.addinivalue_line(
        "markers",
        "allow_unharnessed_subprocess(reason): opt out of the unit-tier "
        "subprocess-isolation guard for a test that must spawn a child rooted at the "
        "real checkout or with a hand-built environment. A non-empty reason is "
        "MANDATORY — `--strict-markers` validates the marker NAME only, so the reason "
        "rule is enforced in tests/_subprocess_isolation.py.",
    )
    config.addinivalue_line(
        "markers",
        "requires_posix_named_semaphore: test needs multiprocessing primitives backed "
        "by POSIX named semaphores; skipped with counted sandbox-compatibility "
        "accounting when that capability is denied.",
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip tests requiring POSIX named semaphores when the host denies them."""
    if item.get_closest_marker("requires_posix_named_semaphore") is None:
        return
    probe = posix_named_semaphore_probe()
    if probe.available:
        return
    reason = probe.reason or "POSIX named semaphore unavailable"
    record_counted_skip(item.config, reason)
    pytest.skip(reason)


class _CountedSkipXdistHooks:
    """Controller-side counted-skip aggregation for pytest-xdist workers."""

    @pytest.hookimpl
    def pytest_testnodedown(self, node: Any, error: object | None) -> None:
        collect_counted_skips_from_worker(node.config, getattr(node, "workeroutput", {}))


def _register_counted_skip_xdist_hooks(config: pytest.Config) -> None:
    """Register xdist-only hooks only when pytest-xdist provides their specs."""
    if not config.pluginmanager.hasplugin("xdist"):
        return
    if config.pluginmanager.hasplugin("rebar-counted-skip-xdist-hooks"):
        return
    config.pluginmanager.register(
        _CountedSkipXdistHooks(),
        name="rebar-counted-skip-xdist-hooks",
    )


_EXTERNAL_DIR = _REPO_ROOT / "tests" / "external"

# Select macOS tests by repo-relative path. Linux runs the full suite. MacOS keeps
# platform-sensitive locking, rename, timeout, and Git-contention coverage.
_PLATFORM_COMPAT_PATH_FRAGMENTS = (
    "/store/",  # store lock / atomic-rename / merge-recovery (unit + interfaces)
    "test_lock",  # advisory / stale / retry / age-ceiling / holder-labelling lock tests
    "test_write_lock",
    "test_stamped_lock",
    "_ref_lock",
    "test_timeout_budget",
    "test_snapshot",
    "test_repo_snapshot",
    "test_fetch_common_dir_coordination",
    "_store_concurrency",
    "test_git_contention",
    "test_event_append_index_lock",
    "test_atomic_ticket_dir_create",
    "test_stale_lock_reclaim",
)


def _is_platform_compat_path(relative_posix: str) -> bool:
    return any(fragment in relative_posix for fragment in _PLATFORM_COMPAT_PATH_FRAGMENTS)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark external tests by directory and reject external marks outside that tier.

    The default selection excludes the whole external tier. Also mark the reduced macOS
    platform_compat selection by path.
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
        if test_path.is_relative_to(_REPO_ROOT) and _is_platform_compat_path(
            test_path.relative_to(_REPO_ROOT).as_posix()
        ):
            item.add_marker("platform_compat")

    if misplaced:
        listing = "\n  ".join(misplaced)
        pytest.fail(
            "External-test confinement violation: the following item(s) are "
            "marked `external` but live OUTSIDE tests/external/. Live/billable "
            "external tests must reside under tests/external/ so the env opt-in "
            "and credential-scoped CI job confine them:\n  " + listing,
            pytrace=False,
        )

    # External Jira tests need loadgroup for xdist_group isolation. Report a UsageError
    # when credentials make them runnable under a scheduler that ignores the mark.
    import _live_jira_confinement

    unconfined = _live_jira_confinement.unconfined_live_jira_reason(config, items)
    if unconfined is not None:
        raise pytest.UsageError(unconfined)

    # Reject unit children whose environment drops the sandbox REBAR_ROOT.
    # UsageError keeps the collection diagnosis legible under xdist.
    import _subprocess_isolation

    unharnessed = _subprocess_isolation.unharnessed_subprocess_reason(
        [item for item in items if _in_unit_tier(item)]
    )
    if unharnessed is not None:
        raise pytest.UsageError(unharnessed)


# The tier whose isolation root the subprocess guard enforces.
_UNIT_TIER = _REPO_ROOT / "tests" / "unit"


def _in_unit_tier(item: pytest.Item) -> bool:
    """Return True if *item* lives under tests/unit/."""
    try:
        return Path(item.fspath).resolve().is_relative_to(_UNIT_TIER)
    except (AttributeError, OSError, ValueError):
        return False


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


_REAL_SOCKET_CONNECT = socket.socket.connect


def _guard_socket_connect(self: socket.socket, *args: object, **kwargs: object) -> object:
    """Block real network (AF_INET/AF_INET6); allow local IPC (e.g. AF_UNIX).

    ``multiprocessing``'s ``forkserver`` start method — the Linux default from Python
    3.14 — reaches its server process over an ``AF_UNIX`` socket, which is local IPC and
    never touches the network. Blocking every address family broke that (and any other
    ``AF_UNIX`` use), so the guard is scoped to the internet families only.
    """
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError(
            "Network access is forbidden in unit/scripts tests. "
            "Mock the network call (e.g. unittest.mock.patch('urllib.request.urlopen')) "
            "or add @pytest.mark.allow_network if this test genuinely needs network access."
        )
    return _REAL_SOCKET_CONNECT(self, *args, **kwargs)


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
    """Recognize coverage.py data files created by pytest-cov workers.

    Other workers may finish during a test's repo-root snapshot. Their gitignored coverage
    files belong to the plugin and must not be reported as test leaks.
    """
    return name == ".coverage" or name.startswith(".coverage.") or name == "coverage.xml"


# Ignore gitignored tool caches created by the framework during tests. Their
# appearance under the repo root is not evidence that a test wrote there.
_TOOLING_CACHE_ARTIFACTS = frozenset(
    {
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".mutmut-cache",
        "__pycache__",
    }
)


def _is_framework_artifact(name: str) -> bool:
    """A framework/tooling artifact under REPO_ROOT is not a test leak.

    Covers coverage.py's data files (``_is_coverage_artifact``) and the gitignored
    tool-cache directories in ``_TOOLING_CACHE_ARTIFACTS`` (pytest/ruff/mypy/mutmut
    caches and ``__pycache__``) — all produced by the toolchain, never authored by a
    test body. Any other new entry is still reported as a potential leak.
    """
    return _is_coverage_artifact(name) or name in _TOOLING_CACHE_ARTIFACTS


@pytest.fixture(autouse=True)
def _no_repo_root_leaks() -> Iterator[None]:
    from _isolation import repo_leak_snapshot as _repo_leak_snapshot

    before = _repo_leak_snapshot(_REPO_ROOT)
    try:
        yield
    finally:
        after = _repo_leak_snapshot(_REPO_ROOT)
        leaked = {name for name in (after - before) if not _is_framework_artifact(name)}
        if leaked:
            # Report new repo-root entries without deleting them. A snapshot proves
            # appearance, not which test or concurrent process wrote the entry.
            pytest.fail(_leak_failure_message(leaked))


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

    def raise_unless_disappeared(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError):
            raise error

    concrete_path = type(tmp_path)
    snapshot_roots = tuple(
        concrete_path(dirpath) / dirname
        for dirpath, dirnames, _filenames in os.walk(
            tmp_path,
            onerror=raise_unless_disappeared,
        )
        if concrete_path(dirpath).name == ".rebar"
        for dirname in dirnames
        if dirname == "run_snapshots"
    )
    with patch.object(snapshot, "Path", concrete_path):
        for snapshot_root in snapshot_roots:
            if snapshot_root.is_dir():
                snapshot._rmtree_writable(snapshot_root)


@pytest.fixture(autouse=True)
def _bound_review_bot_shutdown_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make review-bot hard-shutdown process exit inert during tests.

    A lifespan timer from one test can otherwise kill an unrelated xdist worker. Patching
    the exit seam keeps drain logic testable. Force-down tests replace it with a recorder.
    Skip the patch when the optional review-bot module is unavailable.
    """
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "1.0")
    try:
        from rebar.review_bot import app as _review_bot_app
    except ImportError:
        return
    monkeypatch.setattr(_review_bot_app, "_force_process_exit", lambda code=0: None, raising=False)


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
    """Default repo and gate storage to this worker's sandbox.

    Implicit CLI and child calls must not attach a tracker to the checkout or use host-global
    gate slots. Test-owned fixtures may override REBAR_ROOT or REBAR_GATE_TMPDIR afterward.
    an explicit deletion still selects the cwd fallback.
    """
    monkeypatch.setenv("REBAR_ROOT", str(_rebar_root_sandbox_repo))
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(_rebar_root_sandbox_repo.parent / "gate-store"))


@pytest.fixture(autouse=True)
def _gate_source_attested_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the local HEAD commit as the offline suite's attested gate source.

    Production defaults to origin/main, which disposable test repos may lack. Local-source
    reads cannot sign. HEAD supplies a committed source without a fetch. Tests that need
    in-place reads choose source=local explicitly and expect no signature. Code drift must
    be committed before an attested gate can see it.
    """
    if "REBAR_GATE_SOURCE" not in os.environ:
        monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
        monkeypatch.setenv("REBAR_GATE_REF", "HEAD")


@pytest.fixture(autouse=True)
def _identity_enforcement_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable ambient identity enforcement for in-process tests.

    A host-wide authenticated-authorship flag would break unrelated ticket mutations.
    Dedicated enforcement tests set their own child environment explicitly.
    """
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
    """Disable detached compaction workers in the offline suite by default.

    They can outlive one test and race the next against its temporary store. Tests of the
    trigger set REBAR_COMPACT_TRIGGER explicitly and retain control of the worker.
    """
    if "REBAR_COMPACT_TRIGGER" not in os.environ:
        monkeypatch.setenv("REBAR_COMPACT_TRIGGER", "off")


@pytest.fixture(autouse=True)
def _no_ambient_model_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient model-class overrides before each test.

    Operator settings can change model selection and invalidate tests written against the
    default ladder. Tests exercising overrides set their values after this fixture.
    """
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


# Keep tests in disposable trackers. A nested tracker without its own Git repo can
# commit into this checkout. Writes into existing tracked dirs evade the top-level
# entry guard. Per-test HEAD and session status checks cover both cases. Deliberate
# exceptions use allow_repo_writes. Shared predicates reside in _isolation.py.

from _isolation import head as _repo_head  # noqa: E402
from _isolation import head_move_failure_message as _head_move_failure_message  # noqa: E402
from _isolation import leak_failure_message as _leak_failure_message  # noqa: E402
from _isolation import porcelain as _repo_porcelain  # noqa: E402
from _isolation import working_tree_failure_message as _working_tree_failure_message  # noqa: E402


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
        pytest.fail(_head_move_failure_message(before, after))


# Keep caplog assertions from passing with no records. The rebar parent covers
# library, CLI, MCP, and reconciler logs. A test that disables propagation fails at
# source. An entrypoint that raises the logger level is restored after the test.
# Both guards share _log_integrity primitives with their direct tests.

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
    publish_counted_skips(session.config)
    if _PORCELAIN_AT_START is None:
        return
    after = _repo_porcelain(_REPO_ROOT)
    if after is None:
        return
    leaked = sorted(after - _PORCELAIN_AT_START)
    if not leaked:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    msg = _working_tree_failure_message(leaked)
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line(msg, red=True, bold=True)
    else:  # pragma: no cover - terminalreporter always present under pytest
        print(msg)
    # Escalate the run to a failure so CI catches it even if every test "passed".
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    report_counted_skips(terminalreporter, config)
