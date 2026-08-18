"""Real-Git fixtures for review-bot tickets-clone convergence tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from _subprocess_env import subprocess_env

import rebar

REPO_ROOT = Path(__file__).resolve().parents[2]
ENSURE_SCRIPT = REPO_ROOT / "infra" / "scripts" / "reviewbot-ensure-tickets.sh"


def git(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
    )


def isolated_git_env(home: Path) -> dict[str, str]:
    home.mkdir(exist_ok=True)
    return subprocess_env(
        {
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )


@dataclass(frozen=True)
class StorePair:
    origin: Path
    clone: Path
    home: Path
    old_tip: str


def make_store_pair(tmp_path: Path, monkeypatch) -> StorePair:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-a6-0b")
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ROOT", raising=False)

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "seed@example.com")
    git(origin, "config", "user.name", "seed")
    git(origin, "commit", "-q", "--allow-empty", "-m", "init")
    rebar.init_repo(repo_root=str(origin))
    rebar.create_ticket("task", "seed ticket", repo_root=str(origin))

    old_tip = git(origin, "rev-parse", "tickets").stdout.strip()
    clone = tmp_path / "reviewbot-tickets"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--single-branch",
            "--branch",
            "tickets",
            str(origin),
            str(clone),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=isolated_git_env(home),
    )
    return StorePair(origin=origin, clone=clone, home=home, old_tip=old_tip)


def run_ensure(
    pair: StorePair, *, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = isolated_git_env(pair.home)
    env["REVIEWBOT_TICKETS_DIR"] = str(pair.clone)
    env["REVIEWBOT_PYTHON"] = sys.executable
    env.update(env_overrides or {})
    return subprocess.run(
        ["sh", str(ENSURE_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def rev(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", ref).stdout.strip()


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def commit_local_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        message,
    )
    return rev(repo)


def _commit_tree_with_files(
    repo: Path,
    *,
    tree_source: str,
    parents: list[str],
    files: dict[str, str],
    message: str,
) -> str:
    index = repo / f".test-index-{uuid.uuid4().hex}"
    env = subprocess_env({"GIT_INDEX_FILE": str(index)})
    git(repo, "read-tree", f"{tree_source}^{{tree}}", env=env)
    for path, content in files.items():
        blob = git(repo, "hash-object", "-w", "--stdin", input_text=content).stdout.strip()
        git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, path, env=env)
    tree = git(repo, "write-tree", env=env).stdout.strip()
    args = ["commit-tree", tree]
    for parent in parents:
        args.extend(["-p", parent])
    commit = git(repo, *args, input_text=message + "\n").stdout.strip()
    index.unlink(missing_ok=True)
    return commit


def commit_on_origin(pair: StorePair, name: str, content: str, message: str) -> str:
    old_tip = rev(pair.origin, "tickets")
    commit = _commit_tree_with_files(
        pair.origin,
        tree_source=old_tip,
        parents=[old_tip],
        files={name: content},
        message=message,
    )
    git(pair.origin, "update-ref", "refs/heads/tickets", commit, old_tip)
    return commit


def force_epoch_rewrite(
    pair: StorePair,
    epoch: object,
    *,
    source_tip: str | None = None,
) -> str:
    old_tip = source_tip or rev(pair.origin, "tickets")
    record = json.loads(git(pair.origin, "show", f"{old_tip}:.store-compat.json").stdout)
    record["epoch"] = epoch
    roots = git(
        pair.origin, "rev-list", "--max-parents=0", "--reverse", old_tip
    ).stdout.splitlines()
    assert roots
    commit = _commit_tree_with_files(
        pair.origin,
        tree_source=old_tip,
        parents=[roots[0]],
        files={".store-compat.json": json.dumps(record, sort_keys=True) + "\n"},
        message=f"rewrite epoch {epoch!r}",
    )
    git(pair.origin, "update-ref", "refs/heads/tickets", commit, old_tip)
    return commit


def commit_epoch_on_origin(pair: StorePair, epoch: str) -> str:
    tip = rev(pair.origin, "tickets")
    record = json.loads(git(pair.origin, "show", f"{tip}:.store-compat.json").stdout)
    record["epoch"] = epoch
    return commit_on_origin(
        pair,
        ".store-compat.json",
        json.dumps(record, sort_keys=True) + "\n",
        f"set epoch {epoch}",
    )


def assert_no_merge_or_stash(repo: Path) -> None:
    merge_head = git(repo, "rev-parse", "--verify", "MERGE_HEAD", check=False)
    assert merge_head.returncode != 0
    assert git(repo, "stash", "list").stdout == ""
