"""CLI-interface-specific behaviors (the `rebar` console script).

Covers argv routing, the exit-10 passthrough contract, cwd/REBAR_ROOT
resolution, and reconcile interception — behaviors that the library/MCP tiers
don't exercise.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import rebar


def _cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True, text=True, cwd=cwd,
    )


def test_exit_10_passthrough(rebar_repo: Path) -> None:
    """A rejected transition (valid-but-stale status) must surface the engine's
    exit code 10 through the CLI, unchanged (not collapsed to 1)."""
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    cp = _cli("transition", tid, "in_progress", "closed")
    assert cp.returncode == 10
    # Store unchanged.
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


def test_cwd_independent_resolution(rebar_repo: Path, tmp_path: Path) -> None:
    """Invoked from an unrelated cwd, the CLI still targets REBAR_ROOT."""
    tid = rebar.create_ticket("task", "From elsewhere", repo_root=str(rebar_repo))
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    cp = _cli("show", tid, cwd=str(elsewhere))
    assert cp.returncode == 0
    assert "From elsewhere" in cp.stdout


def test_unknown_subcommand_errors(rebar_repo: Path) -> None:
    cp = _cli("definitely-not-a-command")
    assert cp.returncode != 0
    assert "unknown subcommand" in (cp.stdout + cp.stderr).lower()


def test_help_flag_shows_usage_without_executing(rebar_repo: Path) -> None:
    """`rebar <sub> --help` prints the subcommand usage and exits 0 WITHOUT
    executing the command. `init` is the canonical example: it has no required
    args, so it used to run (and re-initialize) instead of showing help."""
    cp = _cli("init", "--help", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar init" in cp.stdout
    # init prints "initialized" when it actually runs — must be absent here.
    assert "initialized" not in (cp.stdout + cp.stderr).lower()


def test_subcommand_help_has_no_side_effect(rebar_repo: Path) -> None:
    """`rebar create --help` must show usage and create nothing."""
    before = len(rebar.list_tickets(repo_root=str(rebar_repo)))
    cp = _cli("create", "--help", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar create" in cp.stdout
    after = len(rebar.list_tickets(repo_root=str(rebar_repo)))
    assert after == before  # no ticket was created


def test_top_level_help_forms_list_subcommands(rebar_repo: Path) -> None:
    for form in ("--help", "-h", "help"):
        cp = _cli(form, cwd=str(rebar_repo))
        assert cp.returncode == 0, form
        assert "Subcommands:" in cp.stdout, form


def test_help_word_with_subcommand(rebar_repo: Path) -> None:
    cp = _cli("help", "transition", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar transition" in cp.stdout


def test_short_help_flag_on_subcommand(rebar_repo: Path) -> None:
    cp = _cli("tag", "-h", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar tag" in cp.stdout


def test_help_in_freetext_title_is_not_intercepted(rebar_repo: Path) -> None:
    """`--help` appearing in a free-text parameter (the title is at position 2,
    after the type) must NOT trigger help — the ticket is created normally and
    the literal text is preserved."""
    before = len(rebar.list_tickets(repo_root=str(rebar_repo)))
    cp = _cli("create", "task", "please pass --help to the parser", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar create" not in cp.stdout  # not the help text
    after = rebar.list_tickets(repo_root=str(rebar_repo))
    assert len(after) == before + 1
    assert any("--help" in t["title"] for t in after)


def test_bare_help_word_in_freetext_is_not_intercepted(rebar_repo: Path) -> None:
    """The bare word `help` is honored only at top level; as a free-text value
    (a title here) it is stored literally, not treated as a help request."""
    tid = rebar.create_ticket("task", "placeholder", repo_root=str(rebar_repo))
    cp = _cli("comment", tid, "run with --help or -h for usage", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage:" not in cp.stdout  # the comment was added, not help shown
    comments = rebar.show_ticket(tid, repo_root=str(rebar_repo))["comments"]
    assert any("--help" in c["body"] for c in comments)


def test_reconcile_intercepted_dry_run_default(rebar_repo: Path) -> None:
    """`rebar reconcile` is intercepted and routed to the reconciler in dry-run
    by default. Without acli it cannot mutate; it must not crash the CLI with an
    unknown-subcommand error (the dispatcher has no reconcile arm)."""
    cp = _cli("reconcile")
    # Either the reconciler ran (0/75) or it failed cleanly on missing acli — but
    # never the dispatcher's "unknown subcommand 'reconcile'".
    assert "unknown subcommand 'reconcile'" not in (cp.stdout + cp.stderr).lower()
