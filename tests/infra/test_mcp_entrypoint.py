"""Tests for the MCP container entrypoint's code workspace provisioning."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _ROOT / "infra" / "scripts" / "mcp-entrypoint.sh"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


def _init_remote(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "file.txt").write_text("one\n")
    _git(source, "add", "file.txt")
    _git(source, "commit", "-m", "initial")
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(source, "remote", "add", "origin", str(remote))
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


def _run_provision(
    code_dir: Path,
    remote: Path,
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ensure_script = tmp_path / "ensure-store.sh"
    ensure_script.write_text("#!/bin/sh\nexit 0\n")
    env = {
        **os.environ,
        "MCP_CODE_DIR": str(code_dir),
        "MCP_TICKETS_URL": str(remote),
        "REBAR_TRACKER_DIR": str(tmp_path / "tracker"),
        "MCP_ENSURE_SCRIPT": str(ensure_script),
    }
    return subprocess.run(
        ["sh", str(_ENTRYPOINT), "--provision-only"],
        check=False,
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


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
