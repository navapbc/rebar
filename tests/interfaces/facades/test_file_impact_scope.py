"""Happy-path contract for the explicit no-file-impact declaration."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import rebar


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_declares_none_and_library_reads_scope(rebar_repo: Path) -> None:
    ticket_id = rebar.create_ticket(
        "task",
        "Operator-only action",
        repo_root=str(rebar_repo),
    )
    reason = "operator action only"

    result = _cli(
        rebar_repo,
        "set-file-impact",
        ticket_id,
        "--none",
        reason,
    )

    assert result.returncode == 0, result.stderr
    assert rebar.get_file_impact_scope(
        ticket_id,
        repo_root=str(rebar_repo),
    ) == {
        "kind": "none",
        "reason": reason,
        "paths": [],
    }
    assert rebar.get_file_impact(ticket_id, repo_root=str(rebar_repo)) == []
