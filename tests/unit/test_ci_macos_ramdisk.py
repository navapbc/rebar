"""Contract tests for the macOS pytest RAM-disk lifecycle.

The reusable workflow is executable infrastructure: these tests pin the storage-routing
contract while the real Gerrit matrix proves the Apple tooling on a hosted macOS runner.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "_build-and-test.yml"


def _test_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["test"]["steps"]


def _step_running(marker: str) -> dict[str, Any]:
    return next(step for step in _test_steps() if marker in step.get("run", ""))


def _pytest_step(marker: str) -> dict[str, Any]:
    return _step_running(marker)


def test_ramdisk_setup_is_macos_only_bounded_and_captures_exact_device() -> None:
    setup = _step_running("hdiutil attach -nomount")
    body = setup["run"]

    assert setup["if"] == "runner.os == 'macOS'"
    assert setup.get("id"), "pytest routing needs a stable setup output producer"
    size_assignment = re.search(
        r"(?m)^\s*(?P<size>[A-Za-z_]\w*)=\$\(\((?P<gib>[1-9]\d*) \* "
        r"1024 \* 1024 \* 1024\)\)\s*$",
        body,
    )
    physical_assignment = re.search(
        r"(?m)^\s*(?P<physical>[A-Za-z_]\w*)=\$\(sysctl -n hw\.memsize\)\s*$",
        body,
    )
    device_assignment = re.search(
        r"(?m)^\s*(?P<device>[A-Za-z_]\w*)="
        r"\$\{[A-Za-z_]\w*%%\[\[:space:\]\]\*\}\s*$",
        body,
    )
    assert size_assignment and physical_assignment and device_assignment
    assert int(size_assignment.group("gib")) == 2
    size_var = size_assignment.group("size")
    physical_var = physical_assignment.group("physical")
    device_var = device_assignment.group("device")
    sectors_assignment = re.search(
        rf"(?m)^\s*(?P<sectors>[A-Za-z_]\w*)=\$\(\({size_var} / 512\)\)\s*$",
        body,
    )
    assert sectors_assignment
    assert re.search(
        rf"{size_var}\s*\*\s*100\s*>=\s*{physical_var}\s*\*\s*40",
        body,
    )
    assert f"hdiutil attach -nomount ram://${{{sectors_assignment.group('sectors')}}}" in body
    assert re.search(rf'diskutil erasevolume APFS "?RebarTests"? "\${device_var}"', body)
    assert f'echo "device=${device_var}" >> "$GITHUB_OUTPUT"' in body
    assert re.search(r'echo "mount_path=\$[A-Za-z_]\w*" >> "\$GITHUB_OUTPUT"', body)
    assert "/Volumes/" in body
    assert f'hdiutil detach "${device_var}"' in body, (
        "setup failures must detach the exact device they attached"
    )


def test_pytest_storage_changes_only_on_macos_and_preserves_both_tiers() -> None:
    setup_id = _step_running("hdiutil attach -nomount")["id"]
    default = _pytest_step('pytest -m "not integration and not external"')
    integration = _pytest_step("pytest -m integration")
    expected_root = (
        "${{ runner.os == 'macOS' && steps." + setup_id + ".outputs.mount_path || runner.temp }}"
    )

    assert f'--basetemp="{expected_root}/rebar-basetemp"' in default["run"]
    assert f'--basetemp="{expected_root}/rebar-basetemp-int"' in integration["run"]

    assert '-m "not integration and not external"' in default["run"]
    assert "--dist worksteal" in default["run"]
    assert "--cov=rebar --cov-report=term-missing:skip-covered" in default["run"]
    assert "-m integration" in integration["run"]
    assert "--dist loadgroup" in integration["run"]
    for step in (default, integration):
        assert step["env"]["REBAR_REQUIRE_EXTRAS"] == "1"
        assert "matrix.os == 'macos-latest' && '3' || '4'" in step["run"]
        assert "--timeout=300 --timeout-method=thread" in step["run"]


def test_successful_default_tree_is_measured_then_released_before_integration() -> None:
    steps = _test_steps()
    setup_id = _step_running("hdiutil attach -nomount")["id"]
    default = _pytest_step('pytest -m "not integration and not external"')
    integration = _pytest_step("pytest -m integration")
    release = next(
        step
        for step in steps
        if "rm -rf" in step.get("run", "") and "rebar-basetemp" in repr(step.get("env", {}))
    )

    assert steps.index(default) < steps.index(release) < steps.index(integration)
    assert release["if"] == "runner.os == 'macOS'"
    assert "always()" not in release["if"], (
        "a failed default tier must remain available to the always-run final reporter"
    )

    expected_path = f"${{{{ steps.{setup_id}.outputs.mount_path }}}}/rebar-basetemp"
    matching_env = [key for key, value in release.get("env", {}).items() if value == expected_path]
    assert len(matching_env) == 1
    path_var = matching_env[0]
    body = release["run"]
    removal = re.search(rf'rm -rf --? "\$\{{?{re.escape(path_var)}\}}?"', body)
    assert removal, "release must remove only the exact default-suite basetemp"
    assert re.search(r"\bdu\s+-[a-z]*s[a-z]*\s", body)
    assert re.search(r"find\s+.+-type f", body)
    assert body.index("du -") < removal.start()
    assert body.index("find ") < removal.start()
    assert "rebar-basetemp-int" not in repr(release.get("env", {})) + body


def test_ramdisk_cleanup_always_reports_usage_and_detaches_captured_device() -> None:
    setup_id = _step_running("hdiutil attach -nomount")["id"]
    cleanup = next(
        step
        for step in _test_steps()
        if "always()" in step.get("if", "") and "hdiutil detach" in step.get("run", "")
    )
    body = cleanup["run"]
    condition = cleanup["if"]
    serialized_env = repr(cleanup.get("env", {}))

    assert "runner.os == 'macOS'" in condition
    assert f"steps.{setup_id}.outputs.device != ''" in condition
    assert f"steps.{setup_id}.outputs.device" in serialized_env + body
    assert f"steps.{setup_id}.outputs.mount_path" in serialized_env + body
    assert "df -h" in body
    assert re.search(r"\bdu\s+-[a-z]*s[a-z]*\s", body)
    assert re.search(r"find\s+.+-type f", body)
    assert "hdiutil detach" in body
    assert "diskutil list" not in body, "cleanup must never discover a broad detach target"
