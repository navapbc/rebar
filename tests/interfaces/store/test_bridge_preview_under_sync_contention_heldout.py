"""Held-out read-integrity oracle for lock-free bridge preview."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import threading
from pathlib import Path

from sync_contention_harness import _rebar_cli

from rebar._store import lock as tracker_lock


def _load_reconcile_lock():
    """Load the engine lock without claiming the test package's dotted name."""
    source = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "rebar"
        / "_engine"
        / "rebar_reconciler"
        / "_advisory_lock.py"
    )
    spec = importlib.util.spec_from_file_location("_bridge_preview_advisory_lock", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _install_empty_acli(monkeypatch, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    acli = bin_dir / "acli"
    acli.write_text("#!/bin/sh\necho '[]'\nexit 0\n")
    acli.chmod(acli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("JIRA_PROJECT", "DIG")
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USER", "reconciler-tests@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token")
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_NO_SYNC", "1")


def test_preview_is_complete_while_sync_locks_are_held(
    repo_with_origin_tickets, monkeypatch, tmp_path: Path
) -> None:
    """A reader neither waits on nor mutates writer locks and returns complete JSON."""
    repo, tracker, _tid = repo_with_origin_tickets
    _install_empty_acli(monkeypatch, tmp_path)
    reconcile_lock = _load_reconcile_lock()

    acquired = threading.Event()
    release = threading.Event()

    def hold_tracker_lock() -> None:
        handle = tracker_lock.acquire(str(tracker), timeout=30, attempts=1)
        acquired.set()
        release.wait(timeout=60)
        handle.release()

    holder = threading.Thread(target=hold_tracker_lock)
    holder.start()
    lock_oid = reconcile_lock.acquire_pass_lock("concurrent-sync", repo)
    try:
        assert acquired.wait(timeout=10), "could not pre-acquire tracker write lock"
        before_oid = reconcile_lock.check_pass_lock(repo)
        completed = _rebar_cli("bridge", "preview", repo=repo, push="off", timeout=15)

        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["route"] == "preview"
        assert payload["mode"] == "dry-run"
        assert reconcile_lock.check_pass_lock(repo) == before_oid
    finally:
        reconcile_lock.release_pass_lock("concurrent-sync", repo, oid=lock_oid)
        release.set()
        holder.join(timeout=60)
