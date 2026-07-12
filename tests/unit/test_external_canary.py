"""Behavioral tests for the scheduled live-Jira integration canary.

Covers the three moving parts wired for ticket 5f39:

* ``_jira_canary_should_fail`` — the pure fail-on-all-skip decision (this is the
  teeth: it returns True iff the external tier is opted in AND at least one
  ``jira_live`` test was collected but none executed).
* ``pytest_collection_modifyitems`` — auto-marks tests in any module that defines a
  module-level ``_live_jira_ready`` sentinel with the ``jira_live`` marker.
* The workflow config-as-artifact: ``.github/workflows/external-integration.yml``
  carries the weekly schedule, the Jira env wiring, and the acli auth step.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFTEST_PATH = _REPO_ROOT / "tests" / "external" / "conftest.py"
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "external-integration.yml"


def _load_external_conftest() -> types.ModuleType:
    """Import tests/external/conftest.py as a standalone module (no pytest plugin)."""
    spec = importlib.util.spec_from_file_location("_external_conftest_under_test", _CONFTEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_conftest = _load_external_conftest()


# --- the fail-on-all-skip decision (the teeth) -------------------------------


def test_canary_fails_when_all_jira_live_skipped() -> None:
    """Opted in, tests collected, none executed → FAIL the session."""
    assert _conftest._jira_canary_should_fail(2, 0, run_external=True) is True


def test_canary_passes_when_some_jira_live_executed() -> None:
    """Opted in, at least one executed → do not fail."""
    assert _conftest._jira_canary_should_fail(2, 1, run_external=True) is False


def test_canary_noop_when_external_not_opted_in() -> None:
    """Not opted in → never fail, even if everything skipped."""
    assert _conftest._jira_canary_should_fail(2, 0, run_external=False) is False


def test_canary_noop_when_nothing_collected() -> None:
    """No jira_live tests collected at all → not a failure (nothing to validate)."""
    assert _conftest._jira_canary_should_fail(0, 0, run_external=True) is False


# --- auto-marking of Jira-gated modules --------------------------------------


class _FakeItem:
    """Minimal stand-in for a pytest Item exposing .module + .add_marker()."""

    def __init__(self, module: types.ModuleType) -> None:
        self.module = module
        self.markers: list = []

    def add_marker(self, marker) -> None:  # noqa: ANN001 — pytest.MarkDecorator
        self.markers.append(marker)


def _module_with_live_ready() -> types.ModuleType:
    mod = types.ModuleType("_fake_live_jira_module")
    mod._live_jira_ready = lambda: False  # type: ignore[attr-defined]
    return mod


def _module_without_live_ready() -> types.ModuleType:
    return types.ModuleType("_fake_plain_module")


def test_modifyitems_marks_jira_gated_module() -> None:
    """An item whose module defines _live_jira_ready gains the jira_live marker."""
    item = _FakeItem(_module_with_live_ready())
    _conftest.pytest_collection_modifyitems(config=None, items=[item])
    names = {getattr(m, "name", None) for m in item.markers}
    assert "jira_live" in names


def test_modifyitems_skips_plain_module() -> None:
    """An item whose module lacks _live_jira_ready is left unmarked."""
    item = _FakeItem(_module_without_live_ready())
    _conftest.pytest_collection_modifyitems(config=None, items=[item])
    assert item.markers == []


# --- config-as-artifact: the workflow wiring ---------------------------------


def _load_workflow() -> dict:
    return yaml.safe_load(_WORKFLOW_PATH.read_text())


def _on_block(wf: dict) -> dict:
    # PyYAML parses the bare `on:` key as the boolean True; fall back defensively.
    return wf.get(True, wf.get("on", {}))


def test_workflow_has_weekly_schedule() -> None:
    wf = _load_workflow()
    schedule = _on_block(wf).get("schedule")
    assert schedule == [{"cron": "0 6 * * 1"}]


def test_workflow_external_job_wires_jira_env() -> None:
    wf = _load_workflow()
    steps = wf["jobs"]["external"]["steps"]
    # The pytest step carries the Jira env drawn from vars.* / secrets.*.
    envs = [s.get("env", {}) for s in steps if "env" in s]
    pytest_env = next(
        (e for e in envs if "JIRA_URL" in e and "REBAR_RUN_EXTERNAL" in e),
        None,
    )
    assert pytest_env is not None, "no pytest step env with the Jira canary wiring"
    assert pytest_env["JIRA_URL"] == "${{ vars.JIRA_URL }}"
    assert pytest_env["JIRA_USER"] == "${{ vars.JIRA_USER }}"
    assert pytest_env["JIRA_PROJECT"] == "${{ vars.JIRA_PROJECT }}"
    assert pytest_env["JIRA_API_TOKEN"] == "${{ secrets.JIRA_API_TOKEN }}"


def test_workflow_has_acli_auth_step() -> None:
    wf = _load_workflow()
    steps = wf["jobs"]["external"]["steps"]
    run_bodies = [s.get("run", "") for s in steps]
    joined = "\n".join(run_bodies)
    assert 'ln -sf "$HOME/.acli/acli" /usr/local/bin/acli' in joined
    assert "acli jira auth login" in joined
