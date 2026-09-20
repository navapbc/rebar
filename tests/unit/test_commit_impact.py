"""Upstream ref selection for the close gate's stale-clone scan (bug piercing-grained-elver).

``upstream_branch_refs`` used to select remote-tracking refs whose name ended in the LOCAL
branch name, so a clone whose branch was named differently from the branch the commit landed
on saw no upstream history at all and the close gate rejected work that had shipped. The
selection rule is now "every remote-tracking code ref, minus the ticket event-log branch",
which is the exclusion the scan actually exists to make.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from rebar._engine_support.commit_impact import upstream_branch_refs


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(path: Path, branch: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "commit", "--allow-empty", "-q", "-m", "seed")
    return path


def _clone_with_remote_branches(tmp_path: Path, local_branch: str, remotes: list[str]) -> Path:
    upstream = _repo(tmp_path / "upstream", remotes[0])
    for extra in remotes[1:]:
        _git(upstream, "branch", extra)
    local = _repo(tmp_path / "local", local_branch)
    _git(local, "remote", "add", "origin", str(upstream))
    _git(local, "fetch", "-q", "origin")
    return local


def test_upstream_refs_are_found_when_the_local_branch_name_differs(tmp_path: Path) -> None:
    """The regression: a clone on ``master`` must still see ``origin/main``."""
    local = _clone_with_remote_branches(tmp_path, "master", ["main"])

    assert "refs/remotes/origin/main" in upstream_branch_refs(str(local))


def test_upstream_refs_exclude_the_ticket_event_log_branch(tmp_path: Path) -> None:
    """The one exclusion the scan exists to make: ticket events are not code history."""
    local = _clone_with_remote_branches(tmp_path, "main", ["main", "tickets"])

    refs = upstream_branch_refs(str(local))

    assert "refs/remotes/origin/tickets" not in refs
    assert "refs/remotes/origin/main" in refs


def test_upstream_refs_exclude_the_symbolic_remote_head(tmp_path: Path) -> None:
    local = _clone_with_remote_branches(tmp_path, "main", ["main"])
    _git(local, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    assert "refs/remotes/origin/HEAD" not in upstream_branch_refs(str(local))


def test_upstream_refs_include_a_differently_named_code_branch(tmp_path: Path) -> None:
    local = _clone_with_remote_branches(tmp_path, "main", ["main", "release/2026-09"])

    assert "refs/remotes/origin/release/2026-09" in upstream_branch_refs(str(local))


def test_upstream_refs_are_empty_without_a_remote(tmp_path: Path) -> None:
    local = _repo(tmp_path / "solo", "main")

    assert upstream_branch_refs(str(local)) == []


def test_upstream_refs_are_empty_outside_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert upstream_branch_refs(str(plain)) == []
