"""Happy-path oracle for the primary ``rebar bridge`` command group."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from rebar import _cli


def test_bridge_preview_launches_primary_engine_subcommand(rebar_repo: Path, monkeypatch) -> None:
    """Preview crosses the public CLI seam using the primary engine vocabulary."""
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
        "preview",
        "--repo-root",
        str(rebar_repo),
    ]
    assert env is not None
    assert env["REBAR_ROOT"] == str(rebar_repo)


def test_bridge_sync_forwards_primary_operational_options(rebar_repo: Path, monkeypatch) -> None:
    """Sync forwards its cap and selection without translating them to legacy flags."""
    calls: list[list[str]] = []

    def fake_call(argv: list[str], *, env=None) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)

    rc = _cli.main(["bridge", "sync", "--max-changes", "10", "--only", "local-a,DIG-2"])

    assert rc == 0
    assert calls == [
        [
            sys.executable,
            "-m",
            "rebar_reconciler",
            "sync",
            "--max-changes",
            "10",
            "--only",
            "local-a,DIG-2",
            "--repo-root",
            str(rebar_repo),
        ]
    ]
