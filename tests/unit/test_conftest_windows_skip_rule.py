"""The Windows sweep skips tests of the Linux host-operations scripts (bug 6798, task 1ebf).

Those tests drive ``infra/scripts/*.sh`` over PATH stubs of ``systemctl``, ``journalctl`` and
``docker``. The scripts run only on the Linux deploy host and Linux CI already gates them, so a
Windows result carries no information about rebar's Windows behaviour. The rule is a pure
function of the repo-relative path and the platform, so it is pinned here without Windows.
"""

from __future__ import annotations

import importlib.util
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


rule = _conftest()._linux_host_script_skip_reason


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
