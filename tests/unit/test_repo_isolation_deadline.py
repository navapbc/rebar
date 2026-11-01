"""Held-out contract for the nested repo-isolation watchdog harness."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

_GUARD_TEST = Path(__file__).with_name("test_repo_isolation_guard.py")


def _load_guard_test() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_repo_isolation_guard_test", _GUARD_TEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_outer_deadline_leaves_cleanup_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nested process gets headroom after the five-second Git watchdog fires."""
    guard_test = _load_guard_test()
    state = tmp_path / "status.count"
    nested_result = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=1,
        stdout="",
        stderr="subprocess.TimeoutExpired: git status --porcelain exceeded 5 seconds",
    )
    observed_outer_timeouts: list[float] = []

    def fake_blocking_git(_tmp_path: Path, operation: str):
        assert operation == "status"
        return state, {"PATH": "test-path"}

    def completed_nested_pytest(*args, **kwargs):
        observed_outer_timeouts.append(kwargs["timeout"])
        state.write_text("2")
        return nested_result

    monkeypatch.setattr(guard_test, "_blocking_git", fake_blocking_git)
    monkeypatch.setattr(guard_test.subprocess, "run", completed_nested_pytest)
    result = guard_test._run_real_guard_with_blocking_git(tmp_path, "status")

    assert result is nested_result
    assert len(observed_outer_timeouts) == 1
    assert observed_outer_timeouts[0] >= 30


def test_completed_watchdog_result_is_not_rejected_by_aggregate_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup/teardown wall time must not reject an already-completed watchdog result."""
    guard_test = _load_guard_test()
    state = tmp_path / "status.count"
    nested_result = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=1,
        stdout="",
        stderr="subprocess.TimeoutExpired: git status --porcelain exceeded 5 seconds",
    )
    clock = iter((100.0, 113.0))

    def fake_blocking_git(_tmp_path: Path, operation: str):
        assert operation == "status"
        return state, {"PATH": "test-path"}

    def completed_nested_pytest(*args, **kwargs):
        state.write_text("2")
        return nested_result

    monkeypatch.setattr(guard_test, "_blocking_git", fake_blocking_git)
    monkeypatch.setattr(guard_test.subprocess, "run", completed_nested_pytest)
    monkeypatch.setattr(guard_test.time, "monotonic", lambda: next(clock))

    assert guard_test._run_real_guard_with_blocking_git(tmp_path, "status") is nested_result
