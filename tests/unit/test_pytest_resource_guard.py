from __future__ import annotations

import importlib.util
import os
import shutil
import signal
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


def test_registry_lock_reports_contention_without_waiting_forever() -> None:
    guard = _load_module()
    lock_dir = Path.cwd() / ".pytest_cache" / "resource-guard-lock" / str(os.getpid())
    shutil.rmtree(lock_dir.parent, ignore_errors=True)
    try:
        with guard.registry_lock(lock_dir) as first:
            with guard.registry_lock(lock_dir, wait_seconds=0.01) as second:
                assert (first, second) == (True, False)
    finally:
        shutil.rmtree(lock_dir.parent, ignore_errors=True)


def test_signal_cleanup_removes_run_dir_and_terminates_child(monkeypatch: Any) -> None:
    guard = _load_module()
    run_dir = Path.cwd() / ".pytest_cache" / "resource-guard-signal" / str(os.getpid())
    run_dir.mkdir(parents=True)
    killed: list[tuple[int, int]] = []
    handlers: dict[int, Any] = {}

    monkeypatch.setattr(guard.signal, "getsignal", lambda _signum: None)
    monkeypatch.setattr(
        guard.signal,
        "signal",
        lambda signum, handler: handlers.setdefault(signum, handler),
    )
    monkeypatch.setattr(guard.os, "kill", lambda pid, signum: killed.append((pid, signum)))

    guard._install_signal_cleanup(run_dir, [12345])

    with pytest.raises(SystemExit) as exc_info:
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert exc_info.value.code == 128 + signal.SIGTERM
    assert killed == [(12345, signal.SIGTERM)]
    assert not run_dir.exists()


def test_main_registers_workers_strips_separator_and_returns_pytest_exit(
    monkeypatch: Any,
) -> None:
    guard = _load_module()
    calls: list[list[str]] = []
    root = Path.cwd() / ".pytest_cache" / "resource-guard-main" / str(os.getpid())
    shutil.rmtree(root, ignore_errors=True)

    def fake_run_pytest(cmd: list[str], run_dir: Path) -> int:
        calls.append(cmd)
        assert run_dir.is_dir()
        return 7

    try:
        monkeypatch.setattr(guard, "repo_common_dir", lambda: root)
        monkeypatch.setattr(guard, "available_memory_bytes", lambda: 16 * 1024**3)
        monkeypatch.setattr(guard.os, "cpu_count", lambda: 8)
        monkeypatch.setattr(guard, "run_pytest", fake_run_pytest)

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
