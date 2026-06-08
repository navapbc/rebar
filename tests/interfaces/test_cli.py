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


def test_reconcile_intercepted_dry_run_default(rebar_repo: Path) -> None:
    """`rebar reconcile` is intercepted and routed to the reconciler in dry-run
    by default. Without acli it cannot mutate; it must not crash the CLI with an
    unknown-subcommand error (the dispatcher has no reconcile arm)."""
    cp = _cli("reconcile")
    # Either the reconciler ran (0/75) or it failed cleanly on missing acli — but
    # never the dispatcher's "unknown subcommand 'reconcile'".
    assert "unknown subcommand 'reconcile'" not in (cp.stdout + cp.stderr).lower()
