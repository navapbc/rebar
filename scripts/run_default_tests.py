#!/usr/bin/env python3
"""Run the default pytest suite with host-memory-aware xdist sizing."""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

BYTES_PER_WORKER = 4 * 1024**3
# mechanism-ok: lock rebar-default-pytest-registry.lock — ba95-a378-62a5-422c
LOCK_DIR = "rebar-default-pytest-registry.lock"
RUNS_DIR = "rebar-default-pytest-runs"


def choose_worker_count(
    *,
    requested: int,
    available_memory_bytes: int | None,
    cpu_count: int | None,
    active_workers: int,
) -> int:
    """Choose an xdist worker count that degrades under host contention."""
    requested = max(1, requested)
    cpu_budget = max(1, cpu_count or 1)
    memory_budget = (
        cpu_budget
        if available_memory_bytes is None
        else max(1, available_memory_bytes // BYTES_PER_WORKER)
    )
    host_budget = max(1, min(cpu_budget, memory_budget))
    remaining_budget = max(1, host_budget - max(0, active_workers))
    return max(1, min(requested, remaining_budget))


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


@contextmanager
def registry_lock(lock_dir: Path, *, wait_seconds: float = 5.0) -> Iterator[bool]:
    """Briefly serialize registry updates without queueing full test runs."""
    deadline = time.monotonic() + wait_seconds
    acquired = False
    while True:
        try:
            lock_dir.mkdir(parents=True)
            acquired = True
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    try:
        yield acquired
    finally:
        if acquired:
            shutil.rmtree(lock_dir, ignore_errors=True)


def register_run(registry_root: Path, *, workers: int) -> Path:
    registry_root.mkdir(parents=True, exist_ok=True)
    run_dir = registry_root / f"{int(time.time())}-{os.getpid()}"
    run_dir.mkdir()
    metadata = (
        f"pid={os.getpid()}\nworktree={Path.cwd()}\nstarted={time.time()}\nworkers={workers}\n"
    )
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


def active_worker_count(registry_root: Path) -> int:
    if not registry_root.is_dir():
        return 0
    active = 0
    for child in registry_root.iterdir():
        if not child.is_dir():
            continue
        try:
            fields = dict(
                line.split("=", 1)
                for line in (child / "metadata").read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            active += max(1, int(fields.get("workers", "1")))
        except (OSError, ValueError):
            active += 1
    return active


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
    common_dir = repo_common_dir()
    registry_root = common_dir / RUNS_DIR
    with registry_lock(common_dir / LOCK_DIR) as locked:
        if locked:
            prune_stale_runs(registry_root)
        workers = choose_worker_count(
            requested=requested,
            available_memory_bytes=available_memory_bytes(),
            cpu_count=os.cpu_count(),
            active_workers=active_worker_count(registry_root) if locked else requested - 1,
        )
        run_dir = register_run(registry_root, workers=workers)
    _install_signal_cleanup(run_dir)
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
