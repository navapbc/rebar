"""The Windows sweep skips tests of the Linux host-operations scripts (bug 6798, task 1ebf).

Those tests drive ``infra/scripts/*.sh`` over PATH stubs of ``systemctl``, ``journalctl`` and
``docker``. The scripts run only on the Linux deploy host and Linux CI already gates them, so a
Windows result carries no information about rebar's Windows behaviour. The rule is a pure
function of the repo-relative path and the platform, so it is pinned here without Windows.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CONFTEST = Path(__file__).resolve().parents[2] / "tests" / "conftest.py"
TICKET = "1ebf-599a-e15c-465d"

SKIPPED_PATHS = (
    "tests/scripts/test_observability_journal.py",
    "tests/scripts/test_autodeploy_health_gate.py",
    "tests/scripts/test_docker_storage_cap.py",
    "tests/scripts/test_container_cap.py",
    "tests/scripts/test_journald_cap.py",
    "tests/scripts/test_vartmp_cap.py",
    "tests/unit/test_user_data_device_resolution_d614.py",
)


def _conftest():
    spec = importlib.util.spec_from_file_location("_root_conftest_skip_rule", CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT_CONFTEST = _conftest()
rule = _ROOT_CONFTEST._linux_host_script_skip_reason


@pytest.mark.parametrize("path", SKIPPED_PATHS)
def test_each_fragment_is_skipped_on_windows_with_a_reason_naming_the_ticket(path):
    reason = rule(path, "win32")

    assert reason is not None
    assert TICKET in reason


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("path", SKIPPED_PATHS)
def test_nothing_is_skipped_on_linux_or_macos(path, platform):
    assert rule(path, platform) is None


@pytest.mark.parametrize(
    "path",
    [
        "tests/scripts/test_bridge_fsck_repair.py",
        "tests/unit/test_review_bot.py",
        "tests/unit/test_user_data_size_a68c.py",
        "tests/interfaces/store/test_completion_verifier.py",
    ],
)
def test_other_tests_are_not_skipped_on_windows(path):
    assert rule(path, "win32") is None


# The glue: pytest_collection_modifyitems reads sys.platform, computes the item's
# repo-relative path and adds the skip marker. These cases drive the hook itself with a
# fake item, so removing the call fails here even though the pure rule above still passes.

REPO_ROOT = CONFTEST.parents[1]


class _Config:
    def getoption(self, _name, default=None):
        return default


class _LiveJira:
    @staticmethod
    def unconfined_live_jira_reason(config, items):
        return None


class _SubprocessIsolation:
    @staticmethod
    def unharnessed_subprocess_reason(items):
        return None


class _Item:
    def __init__(self, path: Path):
        self.fspath = path
        self.path = path
        self.nodeid = f"{path.relative_to(REPO_ROOT).as_posix()}::test_x"
        self.added = []

    def get_closest_marker(self, _name):
        return None

    def add_marker(self, marker):
        self.added.append(marker)


def _skip_reasons_after_collection(monkeypatch, platform: str) -> list[str]:
    item = _Item(REPO_ROOT / "tests" / "scripts" / "test_vartmp_cap.py")
    monkeypatch.setitem(sys.modules, "_live_jira_confinement", _LiveJira)
    monkeypatch.setitem(sys.modules, "_subprocess_isolation", _SubprocessIsolation)
    monkeypatch.setattr(sys, "platform", platform)
    _ROOT_CONFTEST.pytest_collection_modifyitems(_Config(), [item])
    monkeypatch.undo()
    return [
        marker.mark.kwargs["reason"]
        for marker in item.added
        if isinstance(marker, pytest.MarkDecorator) and marker.mark.name == "skip"
    ]


def test_the_collection_hook_skips_a_host_script_test_on_windows(monkeypatch):
    reasons = _skip_reasons_after_collection(monkeypatch, "win32")

    assert len(reasons) == 1
    assert TICKET in reasons[0]


def test_the_collection_hook_does_not_skip_a_host_script_test_on_linux(monkeypatch):
    assert _skip_reasons_after_collection(monkeypatch, "linux") == []
