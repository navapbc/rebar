"""Happy-path contract for attaching to an advertised tickets branch.

The implementation worker sees this file.  Edge, failure, cache, override, and CLI
contracts live in a physically held-out integration file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar import config
from rebar._commands import init

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def _repo_with_advertised_tickets(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "--initial-branch=tickets", str(remote)],
        check=True,
        capture_output=True,
    )

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "--initial-branch=tickets")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "event.json").write_text("{}\n", encoding="utf-8")
    _git(seed, "add", "event.json")
    _git(seed, "commit", "-q", "-m", "seed tickets")
    seed_sha = _git(seed, "rev-parse", "HEAD").stdout.strip()
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "HEAD:tickets")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "host")
    _git(repo, "remote", "add", "origin", str(remote))
    assert (
        _git(
            repo,
            "show-ref",
            "--verify",
            "refs/remotes/origin/tickets",
            check=False,
        ).returncode
        != 0
    )
    return repo, seed_sha


@pytest.fixture(autouse=True)
def _reset_config() -> None:
    config.reset_config_cache()


def test_remote_advertisement_counts_as_existing_without_tracking_ref(tmp_path: Path) -> None:
    repo, _seed_sha = _repo_with_advertised_tickets(tmp_path)

    assert init.pending_init_attaches_to_existing(repo)
    assert not (repo / ".tickets-tracker").exists()


def test_mount_fetches_advertised_branch_instead_of_orphaning(tmp_path: Path) -> None:
    repo, seed_sha = _repo_with_advertised_tickets(tmp_path)
    tracker = repo / ".tickets-tracker"

    assert init._mount_or_create_branch(str(repo), str(tracker)) == 0
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == seed_sha
    assert (tracker / "event.json").read_text(encoding="utf-8") == "{}\n"
