"""Held-out real-surface checks for parser safety and legacy adapters."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import rebar
from rebar import _engine, _lib_ops
from rebar.mcp_server import build_server


def _tracker_bytes(repo: Path) -> dict[str, bytes]:
    tracker = repo / ".tickets-tracker"
    return {
        str(path.relative_to(tracker)): path.read_bytes()
        for path in tracker.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


@pytest.mark.parametrize("bad_option", ["--dry-run", "--dry-run-en"])
def test_real_engine_rejects_abbreviation_before_store_access(
    rebar_repo: Path, bad_option: str
) -> None:
    before = _tracker_bytes(rebar_repo)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar_reconciler",
            bad_option,
            "--repo-root",
            str(rebar_repo),
        ],
        cwd=rebar_repo,
        env=_engine.engine_env(str(rebar_repo)),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "unrecognized arguments" in completed.stderr
    assert completed.stdout == ""
    assert _tracker_bytes(rebar_repo) == before


def test_library_live_reconcile_keeps_legacy_mode_adapter(rebar_repo: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="OK: applied 1 of 1\n", stderr="")

    monkeypatch.setattr(_lib_ops, "subprocess", SimpleNamespace(run=fake_run))
    result = rebar.reconcile("live", repo_root=str(rebar_repo))

    assert calls[0][-4:] == ["--mode", "live", "--repo-root", str(rebar_repo)]
    assert result == {
        "mode": "live",
        "returncode": 0,
        "output": "OK: applied 1 of 1",
        "stderr": "",
    }


def test_mcp_live_reconcile_keeps_legacy_library_call(monkeypatch) -> None:
    monkeypatch.setenv("REBAR_MCP_ALLOW_JIRA_SYNC", "1")
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        rebar,
        "reconcile",
        lambda mode="dry-run": calls.append(mode) or {"mode": mode, "legacy": True},
    )

    result = asyncio.run(build_server().call_tool("reconcile", {"mode": "live"}))
    assert calls == ["live"]
    assert "legacy" in str(result).lower()
