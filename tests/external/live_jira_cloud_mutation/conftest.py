"""Fixtures and canary enrollment for Jira Cloud mutation probes.

The ``_live_jira_ready`` sentinel gives collected tests the ``jira_live`` marker and enrolls
them in the external tier's all-skip canary. The canary fails a fully skipped enrolled run.
``cloud_client`` owns client construction, and the autouse label sweep removes run-labelled
issues left after primary teardown.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from _cloud_mutation_support import (
    build_cloud_client,
    live_jira_ready,
    run_label,
    run_prefix,
    sweep_label,
    sweep_prefixed_issues,
    sweep_versions,
)

# Sentinel consumed by the parent conftest to auto-mark this suite ``jira_live`` and enrol
# it in the all-skip canary. Its VALUE is irrelevant — only its presence matters.
_live_jira_ready = live_jira_ready


@pytest.fixture
def cloud_client() -> Any:
    """A fresh live-Cloud ``AcliClient``; skips when creds/acli are absent."""
    if not live_jira_ready():
        pytest.skip("no live Jira creds / acli binary")
    return build_cloud_client()


@pytest.fixture
def probe_label() -> str:
    """The run-scoped sweep label shared with the workflow teardown step."""
    return run_label()


@pytest.fixture(autouse=True)
def _label_sweep_backstop(request: pytest.FixtureRequest) -> Iterator[None]:
    """After each test, best-effort delete anything still carrying the run label or prefix.

    Backstop for the per-test by-key ``finally`` teardown: if a test raised between create
    and its own delete, the issue still carries the run label and is swept here; an issue
    created before its run label landed is found by the run prefix in its summary, and
    run-scoped fix versions by the run prefix in their name. No-op when
    the suite is not live (nothing was created). Never raises.
    """
    yield
    if live_jira_ready():
        sweep_label(run_label())
        sweep_prefixed_issues(run_prefix())
        sweep_versions(run_prefix())


@pytest.fixture
def cloud_store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fresh, empty rebar store bound to the Cloud project, for real reconcile passes.

    No remote and no sync push, so nothing it writes leaves the temp dir; the Jira
    credentials come from the job env. Skips when the suite is not live.
    """
    if not live_jira_ready():
        pytest.skip("no live Jira creds / acli binary")
    import subprocess

    from _cloud_mutation_support import cloud_project

    import rebar

    store = tmp_path / "store"
    store.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(store)], check=True)
    for key, value in (("user.email", "rebar-probe@example.com"), ("user.name", "rebar probe")):
        subprocess.run(["git", "-C", str(store), "config", key, value], check=True)
    project = cloud_project()
    (store / "rebar.toml").write_text(
        f'[reconciler]\nbackend = "jira"\n\n[jira]\nproject = "{project}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(store))
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    monkeypatch.setenv("JIRA_PROJECT", project)
    rebar.init_repo(repo_root=str(store), force_new_store=True)
    return store
