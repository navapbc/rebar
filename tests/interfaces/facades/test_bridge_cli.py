"""Happy-path oracle for the staged ``rebar bridge`` command group."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from rebar import _cli


def test_bridge_preview_launches_reconciler_dry_run(rebar_repo: Path, monkeypatch) -> None:
    """Preview crosses the public CLI seam and cannot select a writing mode."""
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_call(argv: list[str], *, env=None) -> int:
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)

    rc = _cli.main(["bridge", "preview"])

    assert rc == 0
    assert len(calls) == 1
    argv, env = calls[0]
    assert argv == [
        sys.executable,
        "-m",
        "rebar_reconciler",
        "--repo-root",
        str(rebar_repo),
        "--mode",
        "dry-run",
    ]
    assert env is not None
    assert env["REBAR_ROOT"] == str(rebar_repo)
