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

# The reader's wall-clock bound IS the lock-freedom oracle, so it must sit strictly between
# two measured anchors. Below: a reader that never touches the writer lock. Above: one that
# waits for it, which cannot finish before the holder's release deadline.
#
#   fast path      ~1.2s  (measured with the writer locks held, on a box at load 9.9)
#   blocking       ~60s   (_HOLD_SECONDS — the holder only releases after this)
#
# The original bound of 15s gave ~12x headroom over the fast path while the signature it must
# distinguish from is ~50x away, so ambient slowness on a constrained CI runner crossed it long
# before anything resembling a block. That is a false "blocked" verdict, not a caught defect.
# Ticket dire-negative-bunting.
_HOLD_SECONDS = 60
_OBSERVED_FAST_PATH_S = 1.5
_MIN_HEADROOM_FACTOR = 20
_PREVIEW_TIMEOUT_S = 45


def test_the_preview_bound_discriminates_blocking_from_ambient_slowness() -> None:
    """The bound must catch a blocked reader AND tolerate a slow machine.

    Without the lower bound, a loaded CI runner fails this suite for reasons unrelated to lock
    behaviour, which erodes CI as a regression oracle (AGENTS.md). Without the upper bound, a
    reader that genuinely waits on the writer lock would slip through and the oracle would be
    worthless. Both directions are load-bearing, so both are asserted.
    """
    assert _PREVIEW_TIMEOUT_S < _HOLD_SECONDS, (
        f"bound {_PREVIEW_TIMEOUT_S}s must stay under the {_HOLD_SECONDS}s blocking signature, "
        "or a reader that waits on the writer lock would pass"
    )
    floor = _MIN_HEADROOM_FACTOR * _OBSERVED_FAST_PATH_S
    assert _PREVIEW_TIMEOUT_S >= floor, (
        f"bound {_PREVIEW_TIMEOUT_S}s gives too little headroom over the ~{_OBSERVED_FAST_PATH_S}s "
        f"fast path; needs >= {floor}s so ambient slowness is not misreported as lock blocking"
    )


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
        completed = _rebar_cli(
            "bridge", "preview", repo=repo, push="off", timeout=_PREVIEW_TIMEOUT_S
        )

        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["route"] == "preview"
        assert payload["mode"] == "dry-run"
        assert reconcile_lock.check_pass_lock(repo) == before_oid
    finally:
        reconcile_lock.release_pass_lock("concurrent-sync", repo, oid=lock_oid)
        release.set()
        holder.join(timeout=60)
