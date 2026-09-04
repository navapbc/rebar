"""Held-out real-surface checks for parser safety and legacy adapters."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

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


def test_library_reconcile_is_removed_before_operational_work(rebar_repo: Path) -> None:
    del rebar_repo  # the stale caller would have supplied it, but lookup fails first.

    assert not hasattr(_lib_ops, "reconcile")
    name = "reconcile"
    with pytest.raises(AttributeError):
        getattr(rebar, name)


def test_mcp_reconcile_is_removed_before_library_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        rebar,
        "reconcile",
        lambda mode="dry-run": calls.append(mode) or {"mode": mode},
        raising=False,
    )

    server = build_server()
    assert "reconcile" not in {tool.name for tool in asyncio.run(server.list_tools())}
    with pytest.raises(Exception) as exc:
        asyncio.run(server.call_tool("reconcile", {"mode": "live"}))
    assert "reconcile" in str(exc.value).lower()
    assert calls == []
