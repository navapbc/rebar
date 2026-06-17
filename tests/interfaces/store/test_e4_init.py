"""init + the auto-init consent gate — cross-surface validation.

Validates the invariant: the ticket store is NEVER created without (a) an explicit
``rebar init`` / :func:`rebar.init_repo`, or (b) an interactive confirmation. Every
path that *can* result in an init is checked: in-process CLI (TTY prompt + non-TTY
error), the library, and the symlink-vs-first-time-init distinction. (MCP exercises
the library write path, which errors here too.)
"""

from __future__ import annotations

import builtins
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._cli import _init as cli_init
from rebar._cli import main


@pytest.fixture
def fresh_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo WITHOUT a rebar tracker (no init)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    return repo


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


# ── init: fresh + idempotent ─────────────────────────────────────────────────
def test_init_fresh_and_idempotent(
    fresh_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # in-process init (cwd = repo, like a user running `rebar init`)
    monkeypatch.chdir(fresh_repo)
    capsys.readouterr()
    code = main(["init"])
    assert code == 0
    assert _tracker(fresh_repo).is_dir()
    branch = subprocess.run(
        ["git", "-C", str(_tracker(fresh_repo)), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "tickets"
    # idempotent
    capsys.readouterr()
    code2 = main(["init"])
    err2 = capsys.readouterr().err
    assert code2 == 0
    assert "already initialized" in err2


# ── consent gate: in-process CLI ─────────────────────────────────────────────
def test_noninteractive_command_errors_without_init(
    fresh_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli_init, "_is_interactive", lambda: False)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["list"])
    assert exc.value.code == 1
    assert "not initialized" in capsys.readouterr().err
    assert not _tracker(fresh_repo).exists()  # NEVER created silently


def test_noninteractive_write_errors_without_init(
    fresh_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli_init, "_is_interactive", lambda: False)
    with pytest.raises(SystemExit):
        main(["create", "task", "x"])
    assert not _tracker(fresh_repo).exists()


@pytest.mark.parametrize("answer", ["y", "yes", ""])  # "" = bare Enter (default Yes)
def test_interactive_yes_initializes(
    fresh_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys, answer: str
) -> None:
    monkeypatch.setattr(cli_init, "_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda: answer)
    capsys.readouterr()
    code = main(["list"])
    assert code == 0
    assert _tracker(fresh_repo).is_dir()


def test_interactive_no_aborts(fresh_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(cli_init, "_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda: "n")
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["list"])
    assert exc.value.code == 1
    assert "Aborted" in capsys.readouterr().err
    assert not _tracker(fresh_repo).exists()


def test_explicit_init_works_noninteractive(
    fresh_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Explicit `rebar init` bypasses the gate even with no TTY."""
    monkeypatch.setattr(cli_init, "_is_interactive", lambda: False)
    monkeypatch.chdir(fresh_repo)
    capsys.readouterr()
    code = main(["init"])
    assert code == 0
    assert _tracker(fresh_repo).is_dir()


# ── symlink vs first-time init: the two concepts are NOT conflated ───────────
def test_worktree_symlinks_silently_without_prompt(
    fresh_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A linked worktree whose MAIN repo is already initialized auto-creates the
    ``.tickets-tracker`` symlink WITHOUT a prompt — even non-interactively —
    because linking to an existing store doesn't change the underlying repo.

    This is the counterpart to ``test_noninteractive_command_errors_without_init``:
    same non-interactive command, but here a store already exists to link to, so
    the consent gate must NOT fire."""
    # First-time init on the main repo (the heavy, consent-worthy step).
    monkeypatch.chdir(fresh_repo)
    capsys.readouterr()
    assert main(["init"]) == 0
    assert _tracker(fresh_repo).is_dir()

    # Add a linked worktree (its .git is a file, not a directory).
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(fresh_repo), "worktree", "add", "-q", str(wt)], check=True)
    assert (wt / ".git").is_file()

    # Drive a read command from the worktree, NON-interactively, with the gate
    # pointed at the worktree. No tracker exists there yet, but it would be a pure
    # symlink to the main repo's store — so it must auto-create, not prompt/error.
    monkeypatch.setattr(cli_init, "_is_interactive", lambda: False)
    monkeypatch.setenv("REBAR_ROOT", str(wt))
    monkeypatch.chdir(wt)
    capsys.readouterr()
    code = main(["list"])
    assert code == 0
    # The worktree's tracker is a SYMLINK resolving to the main repo's store.
    assert (wt / ".tickets-tracker").is_symlink()
    assert (wt / ".tickets-tracker").resolve() == _tracker(fresh_repo).resolve()


def test_worktree_without_main_init_still_errors_noninteractive(
    fresh_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A worktree whose MAIN repo is NOT initialized has no store to link to, so
    there is nothing to symlink — the consent gate still fires (first-time init)."""
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(fresh_repo), "worktree", "add", "-q", str(wt)], check=True)
    monkeypatch.setattr(cli_init, "_is_interactive", lambda: False)
    monkeypatch.setenv("REBAR_ROOT", str(wt))
    monkeypatch.chdir(wt)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["list"])
    assert exc.value.code == 1
    assert "not initialized" in capsys.readouterr().err
    assert not (wt / ".tickets-tracker").exists()


# ── consent gate: library ────────────────────────────────────────────────────
def test_library_write_errors_without_init(fresh_repo: Path) -> None:
    with pytest.raises(rebar.RebarError):
        rebar.create_ticket("task", "x", repo_root=str(fresh_repo))
    assert not _tracker(fresh_repo).exists()


def test_library_read_returns_empty_without_init(fresh_repo: Path) -> None:
    # Reads do not init; they return empty (no silent creation).
    assert rebar.list_tickets(repo_root=str(fresh_repo)) == []
    assert not _tracker(fresh_repo).exists()


def test_library_init_repo_then_writes(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    assert _tracker(fresh_repo).is_dir()
    tid = rebar.create_ticket("task", "now works", repo_root=str(fresh_repo))
    assert tid
