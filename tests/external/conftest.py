"""Fixtures for the external-integration tier (tests/external/).

These tests hit third-party services (live LLM providers, etc.), so they are
marked ``external`` and excluded from the default test run. This conftest provides
the same temp git-backed rebar store the interface tier uses, scoped to this tier
so the suites stay independent.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar


def _env_truthy(name: str) -> bool:
    """True if env var *name* is set to a case-insensitive truthy value."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _jira_canary_should_fail(collected: int, executed: int, run_external: bool) -> bool:
    """Decide whether the scheduled live-Jira canary should FAIL the session.

    The canary exists so a scheduled external-integration run cannot go green while
    every Jira live test silently skipped (missing creds/acli, a broken auth step) —
    an all-skip run validates nothing. Only relevant when the external tier is opted
    in via ``REBAR_RUN_EXTERNAL``; otherwise it is a no-op (returns False). Fails only
    when at least one ``jira_live`` test was collected but NONE executed.
    """
    if not run_external:
        return False
    return collected >= 1 and executed == 0


# nodeids that ran a non-skipped `call` phase this session — populated by
# pytest_runtest_logreport, consumed by pytest_sessionfinish.
_EXECUTED_NODEIDS_KEY = "_rebar_jira_executed_nodeids"


def _executed_set(config: pytest.Config) -> set[str]:
    store = getattr(config, _EXECUTED_NODEIDS_KEY, None)
    if store is None:
        store = set()
        setattr(config, _EXECUTED_NODEIDS_KEY, store)
    return store


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark every Jira-gated module's tests with ``jira_live``.

    Jira live test modules all define a module-level ``_live_jira_ready`` sentinel;
    marking by that presence keeps the canary bookkeeping in one place instead of
    requiring each test to carry the marker by hand.
    """
    for item in items:
        if getattr(item.module, "_live_jira_ready", None) is not None:
            item.add_marker(pytest.mark.jira_live)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record which tests actually EXECUTED (ran a non-skipped call phase).

    A ``call``-phase report means the test body ran (passed or failed); a ``skipped``
    report in setup means it never ran. We record executed nodeids so
    pytest_sessionfinish can tell "ran" from "skipped".
    """
    config = getattr(report, "config", None) or getattr(pytest_runtest_logreport, "_config", None)
    if config is None:
        return
    if report.when == "call" and not report.skipped:
        _executed_set(config).add(report.nodeid)


def pytest_configure(config: pytest.Config) -> None:
    # TestReport carries no back-reference to config, so stash one for logreport.
    pytest_runtest_logreport._config = config  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail a scheduled external run in which every Jira live test skipped.

    No-op unless the external tier is opted in (``REBAR_RUN_EXTERNAL``). Reports the
    collected/executed/skipped counts for ``jira_live``-marked tests, and — when at
    least one was collected but none executed — flips the session to a failure exit.
    """
    if not _env_truthy("REBAR_RUN_EXTERNAL"):
        return
    executed_nodeids = _executed_set(session.config)
    jira_items = [it for it in session.items if it.get_closest_marker("jira_live") is not None]
    collected = len(jira_items)
    executed = sum(1 for it in jira_items if it.nodeid in executed_nodeids)
    skipped = collected - executed
    print(f"\n[jira-live-canary] collected={collected} executed={executed} skipped={skipped}")
    if _jira_canary_should_fail(collected, executed, run_external=True):
        print(
            "[jira-live-canary] FAIL: at least one jira_live test was collected but "
            "none executed (every live-Jira test skipped) — the scheduled canary "
            "validated nothing."
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _require_external_opt_in() -> None:
    """Make every test under tests/external/ INERT unless explicitly opted in.

    External tests hit live third-party services (real Jira mutations, billable
    LLM calls). They must not run during a default suite invocation even when
    credentials happen to be present in the environment — that is the leak this
    guard closes (see bug 4a48-6dd5-aef3-4c8e). This is IN ADDITION to each
    test's own credential skipif: both the opt-in env var AND credentials are
    required for an external test to actually execute.
    """
    if not _env_truthy("REBAR_RUN_EXTERNAL"):
        pytest.skip(
            "external tests are inert by default; set REBAR_RUN_EXTERNAL=1 "
            "(plus the relevant live credentials) to run them"
        )


@pytest.fixture(autouse=True)
def _allow_live_model_requests() -> Iterator[None]:
    """Re-enable live model requests for the external tier.

    The default suite installs a session-scoped safety net
    (``tests/conftest.py::_no_live_model_requests``) that flips pydantic-ai's global
    ``models.ALLOW_MODEL_REQUESTS`` kill-switch to ``False`` so no unit test can
    accidentally bill a provider. The external tier's entire purpose is the opposite —
    it makes REAL, billable calls — so it must flip the switch back on, or every live
    call raises ``RuntimeError: model requests are not allowed`` before any network I/O
    and the external-integration workflow fails without validating anything. Runs after
    the ``_require_external_opt_in`` skip, so it is only active for opted-in runs.
    Guarded — ``pydantic_ai`` is behind the ``[agents]`` extra and absent in lean lanes,
    where this is a no-op. Restores the prior value on teardown.
    """
    try:
        from pydantic_ai import models as _pai_models
    except Exception:  # noqa: BLE001 — agents extra absent (lean lane): nothing to re-enable
        yield
        return
    previous = _pai_models.ALLOW_MODEL_REQUESTS
    _pai_models.ALLOW_MODEL_REQUESTS = True
    try:
        yield
    finally:
        _pai_models.ALLOW_MODEL_REQUESTS = previous


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def rebar_repo(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An initialized rebar repo in a temp git dir (mirrors the interface tier)."""
    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    yield repo
