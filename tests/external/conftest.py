"""Fixtures for the external-integration tier (tests/external/).

These tests hit third-party services (live LLM providers, etc.), so they are
marked ``external`` and excluded from the default test run. This conftest provides
the same temp git-backed rebar store the interface tier uses, scoped to this tier
so the suites stay independent.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import rebar


def _env_truthy(name: str) -> bool:
    """True if env var *name* is set to a case-insensitive truthy value."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _all_skipped_canary_should_fail(collected: int, executed: int, run_external: bool) -> bool:
    """Return whether an opted-in service lane collected tests but ran none.

    The marker-agnostic canary prevents missing credentials or broken authentication from making
    an external Jira or provider lane pass without exercising its service.
    """
    if not run_external:
        return False
    return collected >= 1 and executed == 0


# Backwards-compatible alias: the Jira canary's original name, kept so existing callers/tests
# that reference it by name keep resolving to the (now generalized) predicate.
_jira_canary_should_fail = _all_skipped_canary_should_fail


# nodeids that ran a non-skipped `call` phase this session — populated by
# pytest_runtest_logreport, consumed by pytest_sessionfinish.
_EXECUTED_NODEIDS_KEY = "_rebar_jira_executed_nodeids"


def _executed_set(config: pytest.Config) -> set[str]:
    store = getattr(config, _EXECUTED_NODEIDS_KEY, None)
    if store is None:
        store = set()
        setattr(config, _EXECUTED_NODEIDS_KEY, store)
    return store


# Sentinel names map modules to the external lane selected by CI. Central marking also lets the
# session canary detect a lane where every collected test skipped. ``_live_jira_ready`` selects
# ``jira_live``. ``_live_llm_ready`` selects ``llm_live``. The provider matrix runs the latter,
# while the services job runs its complement.
_SENTINEL_MARKERS = {
    "_live_jira_ready": "jira_live",
    "_live_llm_ready": "llm_live",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark tests from modules carrying a live-tier readiness sentinel."""
    for item in items:
        for sentinel, marker in _SENTINEL_MARKERS.items():
            if getattr(item.module, sentinel, None) is not None:
                item.add_marker(getattr(pytest.mark, marker))


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


# Per-marker canary wording: (marker, canary label, what an all-skip run means).
_CANARIES = (
    (
        "jira_live",
        "jira-live-canary",
        "every live-Jira test skipped — the scheduled canary validated nothing.",
    ),
    (
        "llm_live",
        "llm-live-canary",
        "every live-LLM test skipped — this provider arm called NO model, so its green "
        "result would be indistinguishable from a real pass. Check that the arm's "
        "credential is present for the provider its REBAR_LLM_CONFIG_FILE selects "
        "(see tests/external/_live_llm.py).",
    ),
)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail an opted-in external session when a collected service lane ran no tests.

    The report includes collected, executed, and skipped counts for each lane.
    """
    if not _env_truthy("REBAR_RUN_EXTERNAL"):
        return
    executed_nodeids = _executed_set(session.config)
    for marker, label, meaning in _CANARIES:
        items = [it for it in session.items if it.get_closest_marker(marker) is not None]
        collected = len(items)
        executed = sum(1 for it in items if it.nodeid in executed_nodeids)
        skipped = collected - executed
        print(f"\n[{label}] collected={collected} executed={executed} skipped={skipped}")
        if _all_skipped_canary_should_fail(collected, executed, run_external=True):
            print(
                f"[{label}] FAIL: at least one {marker} test was collected but none "
                f"executed ({meaning})"
            )
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _require_external_opt_in() -> None:
    """Skip external tests unless ``REBAR_RUN_EXTERNAL`` is enabled.

    This prevents credential presence alone from activating third-party mutations or provider
    calls. Each test must also satisfy its service-specific readiness check.
    """
    if not _env_truthy("REBAR_RUN_EXTERNAL"):
        pytest.skip(
            "external tests are inert by default; set REBAR_RUN_EXTERNAL=1 "
            "(plus the relevant live credentials) to run them"
        )


@pytest.fixture(autouse=True)
def _allow_live_model_requests() -> Iterator[None]:
    """Permit model requests only while the opted-in external tier runs.

    The default suite disables pydantic-ai network requests. This fixture restores the prior
    global value after the tier and does nothing when the optional agents package is absent.
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


def build_scratch_rebar_repo(repo: Path) -> Path:
    """Initialize the repository shape shared by external and interface-tier fixtures."""
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    rebar.init_repo(repo_root=str(repo))
    # Give the CODE branch a root commit so the suite-wide attested/``ref=HEAD``
    # gate default (tests/conftest.py) can resolve a snapshot: an unborn HEAD
    # fails ref resolution before any gate op reaches its subject under test.
    _git("commit", "--allow-empty", "-q", "-m", "init", cwd=repo)
    return repo


def write_project_prompt(repo: Path, prompt_id: str, text: str) -> Path:
    """Commit a project prompt override under ``.rebar/prompts``.

    Workflow steps read the attested ``HEAD`` snapshot. An uncommitted override is unavailable
    there and fails before model execution.
    """
    path = repo / ".rebar" / "prompts" / f"{prompt_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git("add", str(path.relative_to(repo)), cwd=repo)
    _git("commit", "-q", "-m", f"add project prompt {prompt_id}", cwd=repo)
    return path


# This plan clears deterministic review checks before the provider-backed phase. Its
# ``## Testing`` section satisfies the verification-presence rule, allowing the fixture to
# prove that a model call occurred.
PLAN_REVIEW_FIXTURE_PLAN = (
    "## Why\nThe in-memory review cache is lost on restart.\n\n"
    "## What\nPersist it under `src/rebar/cache.py` behind the existing seam.\n\n"
    "## Scope\nJust persistence; eviction is out of scope.\n\n"
    "## Testing\nRun `pytest tests/unit/test_cache.py` to prove the round-trip survives a "
    "process restart.\n\n"
    "## Acceptance Criteria\n"
    "- [ ] the cache survives a restart, proved by `pytest tests/unit/test_cache.py`\n"
    "- [ ] the seam writes through to disk\n"
)


@pytest.fixture
def project_prompt_writer() -> Callable[[Path, str, str], Path]:
    """Hand the live tier :func:`write_project_prompt`.

    Exposed as a fixture rather than imported: pytest registers BOTH this conftest and
    the repo-root one under the module name ``conftest``, so a plain
    ``from conftest import …`` in a test module is a collision waiting to happen.
    """
    return write_project_prompt


@pytest.fixture
def plan_review_fixture_plan() -> str:
    """The plan body the live plan-review guard reviews (see PLAN_REVIEW_FIXTURE_PLAN)."""
    return PLAN_REVIEW_FIXTURE_PLAN


@pytest.fixture
def rebar_repo(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An initialized rebar repo in a temp git dir (mirrors the interface tier)."""
    repo = Path(tmp_path) / "repo"
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    build_scratch_rebar_repo(repo)
    return repo
