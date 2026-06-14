"""``init`` idempotently upgrades the ``.scratch/`` exclusions.

In-process port of tests/test-ticket-init-idempotent.sh (the bash engine is being
deleted). Against a repo whose ``.git/info/exclude`` predates the ``.scratch/``
exclusion (legacy entries, no ``.scratch``), running init must:
  * add ``.scratch/`` to the host repo ``.git/info/exclude`` (preserving legacy lines),
  * commit ``.scratch/`` into the tickets-branch ``.gitignore``,
  * add ``.scratch/`` to the tracker worktree's ``.git/info/exclude``,
and a SECOND init must add no duplicates (exactly ONE ``.scratch`` entry in each).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar


@pytest.fixture
def pre_upgrade_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A git repo with a legacy .git/info/exclude (no .scratch), not yet rebar-init'd."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "i@i.i"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "i"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "i"], cwd=repo, check=True)
    info = repo / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").write_text(".tickets-tracker\n.env\n")  # legacy: no .scratch
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    yield repo


def _count(text: str, needle: str = ".scratch") -> int:
    return sum(1 for ln in text.splitlines() if needle in ln)


def _scratch_counts(repo: Path) -> tuple[int, int, int]:
    tracker = repo / ".tickets-tracker"
    main_exclude = (repo / ".git" / "info" / "exclude").read_text()
    gitignore = subprocess.run(
        ["git", "-C", str(tracker), "show", "tickets:.gitignore"],
        capture_output=True, text=True,
    ).stdout
    wt_git = subprocess.run(
        ["git", "-C", str(tracker), "rev-parse", "--git-dir"],
        capture_output=True, text=True,
    ).stdout.strip()
    wt_exclude_path = Path(tracker) / wt_git / "info" / "exclude" if not Path(wt_git).is_absolute() else Path(wt_git) / "info" / "exclude"
    wt_exclude = wt_exclude_path.read_text() if wt_exclude_path.is_file() else ""
    return _count(main_exclude), _count(gitignore), _count(wt_exclude)


def test_init_adds_scratch_exclusions_idempotently(pre_upgrade_repo: Path) -> None:
    repo = pre_upgrade_repo

    rebar.init_repo(repo_root=str(repo))
    assert _scratch_counts(repo) == (1, 1, 1), "first init must add exactly one .scratch each"
    # Legacy entries preserved.
    assert ".tickets-tracker" in (repo / ".git" / "info" / "exclude").read_text()
    assert ".env" in (repo / ".git" / "info" / "exclude").read_text()

    rebar.init_repo(repo_root=str(repo))  # re-run
    assert _scratch_counts(repo) == (1, 1, 1), "second init must not duplicate .scratch"
