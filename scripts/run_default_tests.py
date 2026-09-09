#!/usr/bin/env python3
"""Run the default pytest suite with host-memory-aware xdist sizing."""

from __future__ import annotations

import argparse
import atexit
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

BYTES_PER_WORKER = 4 * 1024**3
# mechanism-ok: lock rebar-default-pytest-run-slot.lock — ba95-a378-62a5-422c
LOCK_FILE = "rebar-default-pytest-run-slot.lock"
RUNS_DIR = "rebar-default-pytest-runs"


def choose_worker_count(
    *,
    requested: int,
    available_memory_bytes: int | None,
    cpu_count: int | None,
    active_runs: int,
    host_slot_acquired: bool = True,
) -> int:
    """Choose an xdist worker count that degrades under host contention."""
    requested = max(1, requested)
    if not host_slot_acquired:
        return 1
    active_runs = max(1, active_runs)
    cpu_budget = max(1, cpu_count or 1)
    memory_budget = (
        cpu_budget
        if available_memory_bytes is None
        else max(1, available_memory_bytes // BYTES_PER_WORKER)
    )
    host_budget = max(1, min(cpu_budget, memory_budget))
    per_run_ceiling = max(1, math.ceil(host_budget / 2))
    fair_share = max(1, host_budget // active_runs)
    return max(1, min(requested, per_run_ceiling, fair_share))


def parse_requested_workers(raw: str) -> int:
    if raw == "auto":
        return max(1, os.cpu_count() or 1)
    try:
        return max(1, int(raw))
    except ValueError as exc:
        message = f"PYTEST_WORKERS must be a positive integer or 'auto', got {raw!r}"
        raise SystemExit(message) from exc


def available_memory_bytes() -> int | None:
    proc_meminfo = Path("/proc/meminfo")
    if proc_meminfo.is_file():
        for line in proc_meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    if sys.platform == "darwin":
        return _darwin_available_memory_bytes()
    return None


def _darwin_available_memory_bytes() -> int | None:
    try:
        page_size = int(subprocess.check_output(["sysctl", "-n", "hw.pagesize"], text=True))
        vm_stat = subprocess.check_output(["vm_stat"], text=True)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    pages = 0
    for line in vm_stat.splitlines():
        label, sep, value = line.partition(":")
        if sep and label in {"Pages free", "Pages inactive", "Pages speculative"}:
            pages += int(value.strip().rstrip("."))
    return pages * page_size


def repo_common_dir() -> Path:
    try:
        raw = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], text=True).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit("could not locate git common dir for pytest run registry") from exc
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def acquire_host_slot(lock_path: Path):
    """Hold a non-blocking full-suite slot, or return ``None`` when another run owns it."""
    try:
        import fcntl
    except ImportError:
        return None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    atexit.register(handle.close)
    return handle


def register_run(registry_root: Path) -> Path:
    registry_root.mkdir(parents=True, exist_ok=True)
    run_dir = registry_root / f"{int(time.time())}-{os.getpid()}"
    run_dir.mkdir()
    metadata = f"pid={os.getpid()}\nworktree={Path.cwd()}\nstarted={time.time()}\n"
    (run_dir / "metadata").write_text(metadata, encoding="utf-8")
    atexit.register(lambda: shutil.rmtree(run_dir, ignore_errors=True))
    return run_dir


def prune_stale_runs(registry_root: Path) -> None:
    if not registry_root.is_dir():
        return
    for run_dir in registry_root.iterdir():
        metadata = run_dir / "metadata"
        try:
            fields = dict(
                line.split("=", 1)
                for line in metadata.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            pid = int(fields["pid"])
        except (OSError, ValueError, KeyError):
            continue
        if _process_exists(pid):
            continue
        shutil.rmtree(run_dir, ignore_errors=True)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def active_run_count(registry_root: Path) -> int:
    if not registry_root.is_dir():
        return 0
    return sum(1 for child in registry_root.iterdir() if child.is_dir())


def _install_signal_cleanup(run_dir: Path) -> None:
    previous: dict[int, object] = {}

    def cleanup(signum: int, _frame: object) -> None:
        shutil.rmtree(run_dir, ignore_errors=True)
        handler = previous.get(signum)
        if callable(handler):
            handler(signum, _frame)
        raise SystemExit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, cleanup)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", default=os.environ.get("PYTEST_WORKERS", "4"))
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    requested = parse_requested_workers(args.workers)
    registry_root = repo_common_dir() / RUNS_DIR
    prune_stale_runs(registry_root)
    host_slot = acquire_host_slot(repo_common_dir() / LOCK_FILE)
    run_dir = register_run(registry_root)
    _install_signal_cleanup(run_dir)
    workers = choose_worker_count(
        requested=requested,
        available_memory_bytes=available_memory_bytes(),
        cpu_count=os.cpu_count(),
        active_runs=active_run_count(registry_root),
        host_slot_acquired=host_slot is not None,
    )
    if workers < requested:
        print(
            f"pytest resource guard: requested {requested} workers, using {workers} "
            "because host memory/active full-suite runs are constrained. If a run is "
            "killed by low memory, treat it as environmental and rerun with "
            "PYTEST_WORKERS=1; clean up only processes from this worktree.",
            file=sys.stderr,
        )
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    return subprocess.run(
        [
            "pytest",
            "-m",
            "not integration and not external",
            "-n",
            str(workers),
            "--dist",
            "worksteal",
            "-q",
            *pytest_args,
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
