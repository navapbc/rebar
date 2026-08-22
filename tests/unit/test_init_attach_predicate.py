"""Happy-path contract for attaching to an advertised tickets branch.

The implementation worker sees this file.  Edge, failure, cache, override, and CLI
contracts live in a physically held-out integration file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar import config
from rebar._cli import _init as cli_init
from rebar._commands import _init_probe, init

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
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


def _repo_with_remote(tmp_path: Path, remote: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "host")
    _git(repo, "remote", "add", "origin", str(remote))
    return repo


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


def test_attach_predicate_is_false_when_reachable_remote_has_no_branch(tmp_path: Path) -> None:
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "--initial-branch=tickets", str(remote)],
        check=True,
        capture_output=True,
    )
    repo = _repo_with_remote(tmp_path, remote)

    assert not init.pending_init_attaches_to_existing(repo)


def test_attach_predicate_is_false_when_remote_is_unreachable(tmp_path: Path) -> None:
    repo = _repo_with_remote(tmp_path, tmp_path / "missing-origin.git")

    assert not init.pending_init_attaches_to_existing(repo)


def test_all_mount_consumers_route_through_one_shared_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    tracker = repo / ".tickets-tracker"
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    config.reset_config_cache()

    probe_calls: list[tuple[str, str, str]] = []

    def _shared_probe(
        repo_arg: str,
        remote: str,
        branch: str,
        **_kwargs: object,
    ) -> _init_probe.RemoteBranchState:
        probe_calls.append((repo_arg, remote, branch))
        return _init_probe.ADVERTISED

    monkeypatch.setattr(_init_probe, "probe_remote_branch", _shared_probe)
    monkeypatch.setattr(_init_probe, "remote_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(init, "_git_ok", lambda *_a, **_k: False)
    monkeypatch.setattr(
        init,
        "_git_fetch",
        lambda *_a, **_k: subprocess.CompletedProcess(["git", "fetch"], 0, "", ""),
    )

    def _git_stub(_cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
        assert not (args and args[0] == "ls-remote"), (
            "a consumer bypassed the shared remote-branch probe"
        )
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(init, "_git", _git_stub)
    init_calls: list[tuple[str, bool, bool]] = []

    def _init_core_spy(
        repo_root: str,
        *,
        silent: bool = False,
        force_new_store: bool = False,
    ) -> int:
        init_calls.append((repo_root, silent, force_new_store))
        return 0

    monkeypatch.setattr(init, "init_core", _init_core_spy)

    assert init._mount_or_create_branch(str(repo), str(tracker)) == 0
    assert init.pending_init_attaches_to_existing(repo)
    cli_init.ensure_store_mounted_best_effort()
    cli_init._create_tracker(str(repo))

    assert probe_calls == [(str(repo), "origin", "tickets")] * 4
    assert init_calls == [(str(repo), True, False), (str(repo), False, False)]
