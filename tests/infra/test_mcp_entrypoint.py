"""Tests for the MCP container entrypoint's code workspace provisioning."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from _git_upkeep import init_bare_remote
from _subprocess_env import subprocess_env

_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _ROOT / "infra" / "scripts" / "mcp-entrypoint.sh"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = subprocess_env(
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_AUTHOR_NAME="Test Author",
        GIT_COMMITTER_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test Author",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


def _init_remote(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "file.txt").write_text("one\n")
    _git(source, "add", "file.txt")
    _git(source, "commit", "-m", "initial")
    remote = init_bare_remote(tmp_path / "remote.git", initial_branch="main")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "origin", "main")
    return source, remote


def _advance(source: Path) -> str:
    (source / "file.txt").write_text("two\n")
    _git(source, "commit", "-am", "advance")
    _git(source, "push", "origin", "main")
    return _git(source, "rev-parse", "HEAD").stdout.strip()


def _clone(remote: Path, checkout: Path) -> None:
    subprocess.run(
        ["git", "clone", str(remote), str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _tree_snapshot(repo: Path) -> dict[str, bytes | str]:
    snapshot: dict[str, bytes | str] = {"HEAD": _head(repo)}
    for path in sorted(repo.rglob("*")):
        if ".git" in path.relative_to(repo).parts or not path.is_file():
            continue
        snapshot[str(path.relative_to(repo))] = path.read_bytes()
    return snapshot


def _path_with_failing_flock(tmp_path: Path) -> str:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/bin/sh\necho fake flock invoked >&2\nexit 127\n")
    fake_flock.chmod(0o755)
    return f"{fake_bin}{os.pathsep}{os.environ['PATH']}"


def _wait_for_file(path: Path, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_with_dir_lock(
    lock_dir: Path,
    label: str,
    *command: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    lock_dir.mkdir(parents=True, exist_ok=True)
    env = subprocess_env(env_overrides)
    return subprocess.run(
        [
            "/bin/sh",
            str(_ENTRYPOINT),
            "--with-dir-lock",
            str(lock_dir),
            label,
            *command,
        ],
        check=False,
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _run_provision(
    code_dir: Path,
    remote: Path,
    tmp_path: Path,
    *,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ensure_script = tmp_path / "ensure-store.sh"
    ensure_script.write_text("#!/bin/sh\nexit 0\n")
    env = subprocess_env(
        MCP_CODE_DIR=str(code_dir),
        MCP_TICKETS_URL=str(remote),
        REBAR_TRACKER_DIR=str(tmp_path / "tracker"),
        MCP_ENSURE_SCRIPT=str(ensure_script),
    )
    if env_overrides is not None:
        env = env.with_overrides(env_overrides)
    return subprocess.run(
        ["/bin/sh", str(_ENTRYPOINT), "--provision-only"],
        check=False,
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_with_dir_lock_uses_python_fcntl_when_flock_binary_unavailable(
    tmp_path: Path,
) -> None:
    result = _run_with_dir_lock(
        tmp_path / "lock",
        "test",
        "/bin/sh",
        "-c",
        "exit 0",
        env_overrides={"PATH": _path_with_failing_flock(tmp_path)},
    )

    assert (result.returncode, "fake flock invoked" in result.stderr) == (0, False)


def test_with_dir_lock_serializes_concurrent_holders(tmp_path: Path) -> None:
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    worker = tmp_path / "worker.sh"
    worker.write_text(
        "#!/bin/sh\n"
        'active="$1/active"\n'
        'if ! mkdir "$active" 2>/dev/null; then echo overlap >> "$1/overlap"; fi\n'
        'echo enter >> "$1/log"\n'
        "sleep 0.3\n"
        'rmdir "$active"\n'
    )
    worker.chmod(0o755)
    command = [
        "/bin/sh",
        str(_ENTRYPOINT),
        "--with-dir-lock",
        str(lock_dir),
        "test",
        str(worker),
        str(state_dir),
    ]

    first = subprocess.Popen(
        command,
        cwd=_ROOT,
        env=subprocess_env(MCP_RECLONE_LOCK_WAIT="5"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command,
        cwd=_ROOT,
        env=subprocess_env(MCP_RECLONE_LOCK_WAIT="5"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_stdout, first_stderr = first.communicate(timeout=10)
    second_stdout, second_stderr = second.communicate(timeout=10)

    assert (
        first.returncode,
        second.returncode,
        (state_dir / "overlap").exists(),
        first_stdout,
        first_stderr,
        second_stdout,
        second_stderr,
    ) == (0, 0, False, "", "", "", "")


def test_with_dir_lock_times_out_on_genuine_contention(tmp_path: Path) -> None:
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            "/bin/sh",
            str(_ENTRYPOINT),
            "--with-dir-lock",
            str(lock_dir),
            "test",
            "/bin/sh",
            "-c",
            f": > {ready}; sleep 2",
        ],
        cwd=_ROOT,
        env=subprocess_env(MCP_RECLONE_LOCK_WAIT="5"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(ready)
        contended = _run_with_dir_lock(
            lock_dir,
            "test",
            "/bin/sh",
            "-c",
            "exit 0",
            env_overrides={"MCP_RECLONE_LOCK_WAIT": "0"},
        )
    finally:
        if holder.poll() is None:
            holder.wait(timeout=5)

    assert (
        contended.returncode,
        "could not acquire the test lock within 0 seconds" in contended.stderr,
        "lock mechanism unavailable" in contended.stderr,
    ) == (1, True, False)


def test_with_dir_lock_releases_after_holder_process_dies(tmp_path: Path) -> None:
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    ready = tmp_path / "ready"
    child_pid = tmp_path / "child.pid"
    holder = subprocess.Popen(
        [
            "/bin/sh",
            str(_ENTRYPOINT),
            "--with-dir-lock",
            str(lock_dir),
            "test",
            "/bin/sh",
            "-c",
            f"echo $$ > {child_pid}; : > {ready}; exec sleep 60",
        ],
        cwd=_ROOT,
        env=subprocess_env(MCP_RECLONE_LOCK_WAIT="5"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(ready)
        _kill_pid(holder.pid)
        holder.wait(timeout=5)
        result = _run_with_dir_lock(
            lock_dir,
            "test",
            "/bin/sh",
            "-c",
            "exit 0",
            env_overrides={"MCP_RECLONE_LOCK_WAIT": "2"},
        )
    finally:
        if child_pid.exists():
            _kill_pid(int(child_pid.read_text().strip()))

    assert result.returncode == 0, result.stderr


def test_with_dir_lock_returns_wrapped_status_and_closes_fd(tmp_path: Path) -> None:
    lock_dir = tmp_path / "lock"

    failed = _run_with_dir_lock(lock_dir, "test", "/bin/sh", "-c", "exit 37")
    reacquired = _run_with_dir_lock(lock_dir, "test", "/bin/sh", "-c", "exit 0")

    assert (failed.returncode, reacquired.returncode, reacquired.stderr) == (37, 0, "")


def test_with_dir_lock_reports_unavailable_mechanism_not_timeout(tmp_path: Path) -> None:
    result = _run_with_dir_lock(
        tmp_path / "lock",
        "test",
        "/bin/sh",
        "-c",
        "exit 0",
        env_overrides={"PATH": "/nonexistent"},
    )

    assert (
        result.returncode,
        "lock mechanism unavailable for the test lock" in result.stderr,
        "could not acquire" in result.stderr,
    ) == (1, True, False)


def test_clean_code_workspace_refresh_does_not_require_flock_binary(tmp_path: Path) -> None:
    source, remote = _init_remote(tmp_path)
    checkout = tmp_path / "code"
    _clone(remote, checkout)
    remote_tip = _advance(source)

    result = _run_provision(
        checkout,
        remote,
        tmp_path,
        env_overrides={"PATH": _path_with_failing_flock(tmp_path)},
    )

    assert (
        result.returncode,
        _head(checkout),
        "fake flock invoked" in result.stderr,
    ) == (0, remote_tip, False)


def test_clean_code_workspace_behind_origin_main_fast_forwards(tmp_path: Path) -> None:
    source, remote = _init_remote(tmp_path)
    checkout = tmp_path / "code"
    _clone(remote, checkout)
    remote_tip = _advance(source)

    result = _run_provision(checkout, remote, tmp_path)

    assert _head(checkout) == remote_tip, result.stderr


def test_dirty_workspace_is_left_byte_identical(tmp_path: Path) -> None:
    source, remote = _init_remote(tmp_path)
    checkout = tmp_path / "code"
    _clone(remote, checkout)
    (checkout / "file.txt").write_text("dirty\n")
    before = _tree_snapshot(checkout)
    _advance(source)

    result = _run_provision(checkout, remote, tmp_path)

    assert (result.returncode, _tree_snapshot(checkout)) == (1, before)


def test_diverged_workspace_is_left_byte_identical(tmp_path: Path) -> None:
    source, remote = _init_remote(tmp_path)
    checkout = tmp_path / "code"
    _clone(remote, checkout)
    (checkout / "file.txt").write_text("local\n")
    _git(checkout, "commit", "-am", "local")
    before = _tree_snapshot(checkout)
    _advance(source)

    result = _run_provision(checkout, remote, tmp_path)

    assert (result.returncode, _tree_snapshot(checkout)) == (1, before)


def test_refusals_distinguish_dirty_diverged_and_unreachable(tmp_path: Path) -> None:
    source, remote = _init_remote(tmp_path)

    dirty = tmp_path / "dirty"
    _clone(remote, dirty)
    (dirty / "file.txt").write_text("dirty\n")
    dirty_result = _run_provision(dirty, remote, tmp_path / "dirty-run")

    diverged = tmp_path / "diverged"
    _clone(remote, diverged)
    (diverged / "file.txt").write_text("local\n")
    _git(diverged, "commit", "-am", "local")
    _advance(source)
    diverged_result = _run_provision(diverged, remote, tmp_path / "diverged-run")

    unreachable = tmp_path / "unreachable"
    _clone(remote, unreachable)
    _git(unreachable, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    unreachable_result = _run_provision(unreachable, remote, tmp_path / "unreachable-run")

    assert (
        "dirty code checkout" in dirty_result.stderr,
        "diverged code checkout" in diverged_result.stderr,
        "origin/main unreachable" in unreachable_result.stderr,
    ) == (True, True, True)


def test_missing_workspace_is_recloned(tmp_path: Path) -> None:
    source, remote = _init_remote(tmp_path)
    remote_tip = _advance(source)
    checkout = tmp_path / "code"

    result = _run_provision(checkout, remote, tmp_path)

    assert (result.returncode, _head(checkout)) == (0, remote_tip)


def test_workspace_with_unresolvable_head_is_recloned(tmp_path: Path) -> None:
    source, remote = _init_remote(tmp_path)
    checkout = tmp_path / "code"
    _clone(remote, checkout)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/missing\n")
    remote_tip = _advance(source)

    result = _run_provision(checkout, remote, tmp_path)

    assert (result.returncode, _head(checkout)) == (0, remote_tip)


def test_unreachable_remote_leaves_workspace_untouched(tmp_path: Path) -> None:
    _, remote = _init_remote(tmp_path)
    checkout = tmp_path / "code"
    _clone(remote, checkout)
    before = _tree_snapshot(checkout)
    _git(checkout, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = _run_provision(checkout, remote, tmp_path)

    assert (result.returncode, _tree_snapshot(checkout)) == (1, before)


def test_refresh_emits_workspace_sha_and_distance_from_target(tmp_path: Path) -> None:
    source, remote = _init_remote(tmp_path)
    checkout = tmp_path / "code"
    _clone(remote, checkout)
    remote_tip = _advance(source)

    result = _run_provision(checkout, remote, tmp_path)

    assert f"code checkout at {remote_tip}; distance from origin/main: ahead 0, behind 0" in (
        result.stderr
    )
