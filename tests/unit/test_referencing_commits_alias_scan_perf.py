"""The close-precheck referencing-commit scan must resolve historical alias
trailers against ONE store pass, not one full-store alias scan per distinct
alias.

`referencing_commits` walks the whole code history and resolves every commit's
`rebar-ticket:` trailer. When those trailers are aliases, the old resolver ran a
fresh full-store alias scan (`os.listdir(tracker)` + a per-ticket CREATE/SNAPSHOT
read) for EACH distinct alias — O(distinct_aliases x store) — which on a large
store turns a single close into tens of minutes of pure JSON parsing. The
contract asserted here is a bounded number of tracker-root scans regardless of
how many distinct aliases the history references, plus unchanged correctness.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._commands import close_precheck

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    _git(repo, "commit", "--allow-empty", "-q", "-m", "root commit")
    return repo


def _new_ticket_alias(repo: Path, title: str) -> str:
    created = rebar.create_ticket("task", title, repo_root=str(repo), return_alias=True)
    return created["alias"]


def test_referencing_commits_scans_the_store_once_for_many_alias_trailers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    tracker = config.tracker_dir(str(repo))

    # The target commit references the ticket under close by ALIAS.
    target_alias = _new_ticket_alias(repo, "the ticket under close")
    _git(repo, "commit", "--allow-empty", "-q", "-m", f"land it\n\nrebar-ticket: {target_alias}")
    target_sha = _git(repo, "rev-parse", "HEAD")
    target_id = rebar.show_ticket(target_alias, repo_root=str(repo))["ticket_id"]

    # A spread of OTHER commits, each referencing a DISTINCT unrelated alias — the
    # exact shape that made the old resolver re-scan the whole store per alias.
    decoy_aliases = [_new_ticket_alias(repo, f"decoy {i}") for i in range(6)]
    for alias in decoy_aliases:
        _git(repo, "commit", "--allow-empty", "-q", "-m", f"other work\n\nrebar-ticket: {alias}")

    tracker_norm = os.path.normpath(str(tracker))
    real_listdir = os.listdir
    root_scans = {"n": 0}

    def counting_listdir(path):
        try:
            if os.path.normpath(os.fspath(path)) == tracker_norm:
                root_scans["n"] += 1
        except (TypeError, ValueError):
            pass
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", counting_listdir)

    found = close_precheck._referencing_commits({target_id}, str(tracker), str(repo))

    # Correctness: the alias-referencing commit is credited to the ticket.
    assert target_sha in found
    # Performance contract: the number of full tracker-root alias scans does NOT
    # grow with the number of distinct alias trailers in history. One pass builds
    # the index; every subsequent resolution is an in-memory lookup.
    assert root_scans["n"] <= 1, (
        f"resolved {1 + len(decoy_aliases)} distinct alias trailers with "
        f"{root_scans['n']} full store scans; expected a single indexed pass"
    )
