from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_default_tests.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_default_tests", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_constrained_available_memory_reduces_requested_workers() -> None:
    guard = _load_module()

    workers = guard.choose_worker_count(
        requested=4,
        available_memory_bytes=5 * 1024**3,
        cpu_count=8,
        active_workers=0,
    )

    assert workers == 1


def test_active_full_suite_workers_reduce_available_share() -> None:
    guard = _load_module()

    workers = guard.choose_worker_count(
        requested=4,
        available_memory_bytes=16 * 1024**3,
        cpu_count=8,
        active_workers=3,
    )

    assert workers == 1


def test_exhausted_worker_budget_falls_back_to_serial_workers() -> None:
    guard = _load_module()

    workers = guard.choose_worker_count(
        requested=4,
        available_memory_bytes=64 * 1024**3,
        cpu_count=8,
        active_workers=7,
    )

    assert workers == 1


def test_parse_workers_accepts_auto_and_rejects_invalid(monkeypatch: Any) -> None:
    guard = _load_module()
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 6)

    assert guard.parse_requested_workers("auto") == 6

    with pytest.raises(SystemExit) as exc_info:
        guard.parse_requested_workers("many")
    assert "PYTEST_WORKERS" in str(exc_info.value)


def test_stale_registry_entries_do_not_reduce_workers() -> None:
    guard = _load_module()
    root = Path.cwd() / ".pytest_cache" / "resource-guard-tests"
    registry = root / str(os.getpid())
    shutil.rmtree(root, ignore_errors=True)
    try:
        live = registry / "live"
        stale = registry / "stale"
        live.mkdir(parents=True)
        stale.mkdir()
        (live / "metadata").write_text(f"pid={os.getpid()}\nworkers=2\n", encoding="utf-8")
        (stale / "metadata").write_text("pid=999999999\n", encoding="utf-8")

        guard.prune_stale_runs(registry)

        assert sorted(path.name for path in registry.iterdir()) == ["live"]
        assert guard.active_worker_count(registry) == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_main_registers_workers_strips_separator_and_returns_pytest_exit(
    monkeypatch: Any,
) -> None:
    guard = _load_module()
    calls: list[list[str]] = []
    root = Path.cwd() / ".pytest_cache" / "resource-guard-main" / str(os.getpid())
    shutil.rmtree(root, ignore_errors=True)

    def fake_run(cmd: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert check is False
        return subprocess.CompletedProcess(cmd, 7)

    try:
        monkeypatch.setattr(guard, "repo_common_dir", lambda: root)
        monkeypatch.setattr(guard, "available_memory_bytes", lambda: 16 * 1024**3)
        monkeypatch.setattr(guard.os, "cpu_count", lambda: 8)
        monkeypatch.setattr(guard.subprocess, "run", fake_run)

        assert guard.main(["--workers", "4", "--", "-k", "resource_guard"]) == 7

        assert calls == [
            [
                "pytest",
                "-m",
                "not integration and not external",
                "-n",
                "4",
                "--dist",
                "worksteal",
                "-q",
                "-k",
                "resource_guard",
            ]
        ]
        registry = root / guard.RUNS_DIR
        run_dirs = list(registry.iterdir())
        assert len(run_dirs) == 1
        assert "workers=4" in (run_dirs[0] / "metadata").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)
