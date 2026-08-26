"""Fixtures + canary enrolment for the bounded live-Cloud coordinator MUTATION probe.

Defining the module-level ``_live_jira_ready`` sentinel here earns every test in this suite
the ``jira_live`` marker from the parent ``tests/external/conftest.py``
(``pytest_collection_modifyitems``), which in turn enrols the suite in the all-skip canary:
if these tests are COLLECTED under ``REBAR_RUN_EXTERNAL=1`` but every one SKIPS (missing
creds / broken acli auth), the session FAILS rather than reporting a hollow green — a
missing secret can never masquerade as a passing Live-External AC.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from _cloud_mutation_support import (
    build_cloud_client,
    live_jira_ready,
    run_label,
    sweep_label,
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
    """After each test, best-effort delete anything still carrying the run label.

    Backstop for the per-test by-key ``finally`` teardown: if a test raised between create
    and its own delete, the issue still carries the run label and is swept here. No-op when
    the suite is not live (nothing was created). Never raises.
    """
    yield
    if live_jira_ready():
        sweep_label(run_label())
