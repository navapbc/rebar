"""Library ``bridge_sync`` runs behind the same guard sequence as the CLI.

Story unadhesive-caloric-sapsucker: ``rebar.bridge_sync`` (and so the MCP
``bridge_sync`` tool) used to call the pass body directly, so a library or MCP live
pass took no lease, ran no heartbeat, published no last-pass witness, and ignored the
pause ref. These tests drive the real library entry point against a real store and a
stubbed pass body, and observe the reconciler's refs from inside and after the pass.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

import rebar


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def origin(rebar_repo: Path, tmp_path: Path) -> Path:
    """Give the store the configured sync remote the bridge control refs live on."""
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(rebar_repo, "remote", "add", "origin", str(remote))
    _git(rebar_repo, "push", "-u", "origin", "HEAD")
    _git(rebar_repo, "config", "user.email", "ops@example.com")
    return remote


def _advisory():
    return importlib.import_module("rebar.reconciler._advisory_lock")


def _lock_held(repo: Path) -> bool:
    advisory = _advisory()
    ref_lock = advisory._load_ref_lock()
    return ref_lock.read(repo, ref_lock.LOCK_REF, remote=advisory._lock_remote(repo)) is not None


def _last_pass(repo: Path) -> dict:
    last_pass = importlib.import_module("rebar.reconciler.last_pass")
    return last_pass.snapshot(repo)


@pytest.fixture
def stub_pass(monkeypatch: pytest.MonkeyPatch):
    """Stub the pass body and the project preflight; record what the body observed."""
    observed: dict[str, object] = {"calls": 0}
    reconcile = importlib.import_module("rebar.reconciler.reconcile")
    preflight = importlib.import_module("rebar.reconciler._preflight")
    monkeypatch.setattr(preflight, "project_visibility_preflight", lambda *_a, **_k: None)

    def fake_reconcile_once(pass_id, *, repo_root, abort_check=None, **_kwargs):
        observed["calls"] = int(observed["calls"]) + 1
        observed["lock_held_during_pass"] = _lock_held(Path(repo_root))
        observed["abort_check_callable"] = callable(abort_check)
        if observed.get("raise") is not None:
            raise observed["raise"]  # type: ignore[misc]
        return {"pass_id": pass_id, "mutation_count": 0, "mutations_applied": 0}

    monkeypatch.setattr(reconcile, "reconcile_once", fake_reconcile_once)
    return observed


def test_bridge_sync_holds_the_pass_lock_and_publishes_success(rebar_repo: Path, stub_pass):
    result = rebar.bridge_sync()

    assert result["state"] == "converged"
    assert result["returncode"] == 0
    assert stub_pass["calls"] == 1
    assert stub_pass["lock_held_during_pass"] is True
    assert stub_pass["abort_check_callable"] is True
    assert not _lock_held(rebar_repo), "the lease must be released after the pass"
    assert _last_pass(rebar_repo)["outcome"] == "success"


def test_bridge_sync_yields_to_a_live_pass_lock(
    rebar_repo: Path, stub_pass, monkeypatch: pytest.MonkeyPatch
):
    # With the steal kill-switch off a held lock yields at once, exactly as the CLI does;
    # with it on, the steal path first observes the holder's heartbeat over its lease.
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_STEAL", "0")
    advisory = _advisory()
    ref_lock = advisory._load_ref_lock()
    ref_lock.acquire(
        rebar_repo,
        ref_lock.LOCK_REF,
        holder="other-pass",
        lease_secs=600,
        remote=advisory._lock_remote(rebar_repo),
    )

    result = rebar.bridge_sync()

    assert result["state"] == "in-flight"
    assert result["returncode"] == 0
    assert stub_pass["calls"] == 0


def test_bridge_sync_honours_an_active_pause(rebar_repo: Path, origin: Path, stub_pass):
    rebar.bridge_pause("maint")

    result = rebar.bridge_sync()

    assert result["state"] == "paused"
    assert result["returncode"] == 0
    assert stub_pass["calls"] == 0


def test_a_rescheduled_sync_pass_publishes_reschedule(rebar_repo: Path, stub_pass):
    applier = importlib.import_module("rebar.reconciler.applier")
    stub_pass["raise"] = applier.RescheduleError(3, "head drift")

    result = rebar.bridge_sync()

    assert result["state"] == "reschedule"
    assert result["returncode"] == 0
    record = _last_pass(rebar_repo)
    assert record["outcome"] == "failure"
    assert record["failure_kind"] == "reschedule"
    assert record["detail"]["process_exit_code"] == 0
    assert not _lock_held(rebar_repo)


def test_a_rescheduled_cli_live_pass_publishes_reschedule(rebar_repo: Path, stub_pass):
    applier = importlib.import_module("rebar.reconciler.applier")
    main = importlib.import_module("rebar.reconciler.__main__")
    stub_pass["raise"] = applier.RescheduleError(3, "head drift")

    rc = main.main(["--mode", "live", "--repo-root", str(rebar_repo)])

    assert rc == 75
    record = _last_pass(rebar_repo)
    assert record["failure_kind"] == "reschedule"
    assert record["detail"]["process_exit_code"] == 75
