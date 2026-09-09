from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

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
        active_runs=1,
    )

    assert workers == 1


def test_active_full_suite_runs_reduce_fair_worker_share() -> None:
    guard = _load_module()

    workers = guard.choose_worker_count(
        requested=4,
        available_memory_bytes=16 * 1024**3,
        cpu_count=8,
        active_runs=3,
    )

    assert workers == 1


def test_busy_host_slot_falls_back_to_serial_workers() -> None:
    guard = _load_module()

    workers = guard.choose_worker_count(
        requested=4,
        available_memory_bytes=64 * 1024**3,
        cpu_count=8,
        active_runs=1,
        host_slot_acquired=False,
    )

    assert workers == 1


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
        (live / "metadata").write_text(f"pid={os.getpid()}\n", encoding="utf-8")
        (stale / "metadata").write_text("pid=999999999\n", encoding="utf-8")

        guard.prune_stale_runs(registry)

        assert sorted(path.name for path in registry.iterdir()) == ["live"]
    finally:
        shutil.rmtree(root, ignore_errors=True)
