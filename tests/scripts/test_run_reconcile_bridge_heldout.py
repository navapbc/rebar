"""Held-out edge and recovery oracle for the runner-neutral bridge entrypoint."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env
from test_run_reconcile_bridge import (
    ROOT,
    bridge_workspace,
    git,
    run_bridge,
    runner_env,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("legacy_rc", [3, 4, 75])
def test_legacy_benign_results_translate_only_at_runner_boundary(
    tmp_path: Path, legacy_rc: int
) -> None:
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout, mode="reconcile-check")
    env["STUB_RC"] = str(legacy_rc)

    completed = run_bridge(checkout, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert git(origin, "show", "tickets:bridge-event.txt").stdout == "event from bridge\n"


@pytest.mark.parametrize("rc", [1, 2])
def test_operational_and_invocation_failures_are_preserved_without_delivery(
    tmp_path: Path, rc: int
) -> None:
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    before = git(origin, "rev-parse", "tickets").stdout.strip()
    env = runner_env(tmp_path, checkout)
    env.update({"STUB_RC": str(rc), "STUB_STDERR": "specific subprocess diagnostic\n"})

    completed = run_bridge(checkout, env)

    assert completed.returncode == rc
    assert "specific subprocess diagnostic" in completed.stderr
    assert git(origin, "rev-parse", "tickets").stdout.strip() == before


def test_exact_pause_marker_is_benign_and_skips_delivery(tmp_path: Path) -> None:
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    before = git(origin, "rev-parse", "tickets").stdout.strip()
    env = runner_env(tmp_path, checkout)
    env["STUB_MUTATE"] = "0"
    env["STUB_STDERR"] = (
        'BRIDGE_PAUSED: {"paused":true,"reason":"cutover","who":"operator@example.com",'
        '"paused_at":"2026-08-10T12:00:00Z"}\n'
    )

    completed = run_bridge(checkout, env)

    assert completed.returncode == 0
    assert "Reconcile bridge is paused; skipping ticket commit and push." in completed.stdout
    assert git(origin, "rev-parse", "tickets").stdout.strip() == before


@pytest.mark.parametrize(
    "marker",
    [
        "BRIDGE_PAUSED: {not-json}\n",
        'prefix BRIDGE_PAUSED: {"paused":true}\n',
        'BRIDGE_PAUSED: {"paused":false,"reason":"r","who":"w","paused_at":"t"}\n',
        'BRIDGE_PAUSED: {"paused":true,"reason":"r","who":"w"}\n',
    ],
)
def test_malformed_or_inexact_pause_markers_do_not_suppress_delivery(
    tmp_path: Path, marker: str
) -> None:
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout)
    env["STUB_STDERR"] = marker

    completed = run_bridge(checkout, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert git(origin, "show", "tickets:bridge-event.txt").stdout == "event from bridge\n"


@pytest.mark.parametrize(
    "missing",
    [
        "MODE",
        "BRIDGE_RUN_ID",
        "BRIDGE_BOT_NAME",
        "BRIDGE_BOT_EMAIL",
        "JIRA_URL",
        "JIRA_USER",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT",
        "REBAR_ENV_ID",
    ],
)
def test_required_delivery_identity_is_validated_before_reconciliation(
    tmp_path: Path, missing: str
) -> None:
    checkout, _tracker, origin = bridge_workspace(tmp_path)
    before = git(origin, "rev-parse", "tickets").stdout.strip()
    env = runner_env(tmp_path, checkout)
    del env[missing]

    completed = run_bridge(checkout, env)

    assert completed.returncode == 2
    assert missing in completed.stderr
    assert not Path(env["REBAR_ARGV_FILE"]).exists()
    assert git(origin, "rev-parse", "tickets").stdout.strip() == before


def test_unknown_mode_is_invalid_before_reconciliation(tmp_path: Path) -> None:
    checkout, _tracker, _origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout, mode="surprise")

    completed = run_bridge(checkout, env)

    assert completed.returncode == 2
    assert "Unknown bridge profile: surprise" in completed.stderr
    assert not Path(env["REBAR_ARGV_FILE"]).exists()


def test_shallow_checkout_is_rejected_before_reconciliation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(source, "config", "user.name", "Seed")
    git(source, "config", "user.email", "seed@example.com")
    (source / "one").write_text("one\n", encoding="utf-8")
    git(source, "add", "one")
    git(source, "commit", "-m", "one")
    (source / "two").write_text("two\n", encoding="utf-8")
    git(source, "add", "two")
    git(source, "commit", "-m", "two")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth=1", source.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
        text=True,
    )
    env = subprocess_env()
    env.pop("REBAR_ROOT", None)
    env.update(
        {
            "MODE": "live",
            "BRIDGE_RUN_ID": "run-123",
            "BRIDGE_BOT_NAME": "Bridge Bot",
            "BRIDGE_BOT_EMAIL": "bridge@example.com",
            "JIRA_URL": "https://jira.example.test",
            "JIRA_USER": "bridge@example.com",
            "JIRA_API_TOKEN": "secret",
            "JIRA_PROJECT": "RB",
            "REBAR_ENV_ID": "reconciler",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "bridge", "run"],
        cwd=shallow,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "shallow" in completed.stderr.lower()
    assert "full history" in completed.stderr.lower()


def test_strict_delivery_failure_is_fatal_and_preserves_diagnostic(tmp_path: Path) -> None:
    checkout, tracker, _origin = bridge_workspace(tmp_path)
    env = runner_env(tmp_path, checkout)
    git(tracker, "remote", "set-url", "origin", str(tmp_path / "missing-origin.git"))

    completed = run_bridge(checkout, env)

    assert completed.returncode == 1
    assert "missing-origin" in completed.stderr or "does not appear to be a git repository" in (
        completed.stderr
    )


def test_legacy_engine_dispositions_remain_visible_to_direct_callers() -> None:
    sys.path.insert(0, str(ROOT / "src" / "rebar" / "_engine"))
    try:
        from rebar_reconciler.__main__ import _Disposition
    finally:
        sys.path.pop(0)

    assert _Disposition.IN_FLIGHT.legacy_exit == 3
    assert _Disposition.PHASE_GATE.legacy_exit == 4
    assert _Disposition.RESCHEDULE.legacy_exit == 75
    assert {
        _Disposition.OPERATIONAL_FAILURE.canonical_exit,
        _Disposition.INVALID_INVOCATION.canonical_exit,
    } == {
        1,
        2,
    }
