"""``bridge_status`` against a store RELOCATED outside the repo root (bug 0514-92e0).

Relocation is a documented feature, not an edge case: ``config.tracker_dir()`` states that
``REBAR_TRACKER_DIR`` "wins verbatim" and that an absolute ``tracker.dir`` "relocates the store
(EV-3b)". The reconciler's last-pass reader composed ``repo_root / ".tickets-tracker"`` instead,
so every bridge status read on a relocated deployment failed naming a path nobody configured.

The relocated case is asserted against a CONTROL arm in the default in-tree layout. Without the
control a failure here is ambiguous — it could mean the fixture is broken rather than that the
resolver is bypassed — and the control is also the AC16 no-regression guard for the common case.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar

LAST_PASS_REF = "refs/reconciler/last-pass"


def _relocate(repo: Path, destination: Path) -> Path:
    """Move the store out of the repo root, returning its new home.

    Asserts the move actually happened in BOTH directions: the store is present at the
    destination AND absent from the repo root. Without the second assertion a resolver that
    still reads ``repo_root/.tickets-tracker`` would find a leftover store and pass.
    """
    in_tree = repo / ".tickets-tracker"
    assert (in_tree / ".env-id").is_file(), f"fixture built no store at {in_tree}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(in_tree), str(destination))
    assert (destination / ".env-id").is_file(), "store did not arrive at the destination"
    assert not in_tree.exists(), "store must NOT remain at the repo root"
    return destination


def _plant_last_pass(repo: Path, environment_id: str) -> None:
    payload = {
        "schema_version": 2,
        "pass_id": "pass-relocated",
        "environment_id": environment_id,
        "outcome": "success",
        "completed_at": "2026-08-09T12:00:00Z",
        "lock_fence": 3,
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    oid = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=raw,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", LAST_PASS_REF, oid],
        capture_output=True,
        check=True,
    )


@pytest.fixture(autouse=True)
def _no_ambient_env_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """``resolve_environment_id`` short-circuits on ``REBAR_ENV_ID``.

    That branch returns before the store is ever touched, so an ambient value would make
    this module pass without exercising the defect at all.
    """
    monkeypatch.delenv("REBAR_ENV_ID", raising=False)


def test_bridge_status_reads_env_id_from_a_relocated_store(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC9/AC15: the env id comes from the CONFIGURED store, not the repo root."""
    store = _relocate(rebar_repo, tmp_path / "outside" / "mcp-tickets")
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(store))
    local_id = (store / ".env-id").read_text().strip()
    _plant_last_pass(rebar_repo, f"local:{local_id}")

    status = rebar.bridge_status(repo_root=str(rebar_repo))

    assert status["target_environment_id"] == f"local:{local_id}"
    assert status["verdict"] == "HEALTHY"
    assert status["pass_id"] == "pass-relocated"


def test_bridge_status_error_never_names_the_unconfigured_repo_root_path(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported failure mode: an error naming a path the operator never configured.

    Asserted separately from the happy path because it is the OBSERVABLE the live report
    carried, and because a fix that resolves the path but still reports the old one in its
    error text would leave the operator debugging the same wrong directory.
    """
    store = _relocate(rebar_repo, tmp_path / "outside" / "mcp-tickets")
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(store))
    (store / ".env-id").unlink()  # force the read to fail so an error path is produced

    with pytest.raises(rebar.RebarError) as excinfo:
        rebar.bridge_status(repo_root=str(rebar_repo))

    message = str(excinfo.value)
    assert str(rebar_repo / ".tickets-tracker") not in message, (
        f"error names the UNCONFIGURED repo-root path: {message}"
    )
    assert str(store) in message, f"error should name the configured store: {message}"


def test_bridge_status_resolves_the_last_pass_detail_file_in_a_relocated_store(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8: ``DETAIL_RELATIVE`` is joined under the CONFIGURED store.

    Detail is optional — a missing file degrades to ``detail_status == "missing"`` rather
    than raising — so a test that only planted the detail file could not distinguish "read
    from the right place" from "read from the wrong place and silently degraded". This
    plants a MATCHING detail record in the relocated store and asserts it is actually
    consumed, which only holds if the join resolved there.
    """
    store = _relocate(rebar_repo, tmp_path / "outside" / "mcp-tickets")
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(store))
    local_id = (store / ".env-id").read_text().strip()
    _plant_last_pass(rebar_repo, f"local:{local_id}")

    detail_dir = store / ".bridge_state"
    detail_dir.mkdir(parents=True, exist_ok=True)
    (detail_dir / "last-pass.json").write_text(
        json.dumps(
            {
                "pass_id": "pass-relocated",
                "environment_id": f"local:{local_id}",
                "process_exit_code": 0,
            }
        ),
        encoding="utf-8",
    )

    status = rebar.bridge_status(repo_root=str(rebar_repo))

    assert status["detail_status"] == "matching"
    assert status["detail"]["process_exit_code"] == 0


def test_bridge_status_ignores_stale_reconcile_check_summary_from_configured_store(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _relocate(rebar_repo, tmp_path / "outside" / "mcp-tickets")
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(store))
    local_id = (store / ".env-id").read_text().strip()
    _plant_last_pass(rebar_repo, f"local:{local_id}")

    detail_dir = store / ".bridge_state"
    detail_dir.mkdir(parents=True, exist_ok=True)
    (detail_dir / "reconcile-check.json").write_text(
        json.dumps(
            {
                "total_bindings": 6,
                "checked": 5,
                "in_sync": 4,
                "discrepancies": [{"jira_key": "REB-1", "local_id": "loc-1", "field": "title"}],
                "orphaned_bindings": ["loc-gone"],
                "orphaned_jira": ["REB-9", "REB-10"],
                "unbound_local": 3,
                "unbound_jira": 7,
            }
        ),
        encoding="utf-8",
    )

    status = rebar.bridge_status(repo_root=str(rebar_repo))

    assert "reconcile_diagnostics" not in status


def test_bridge_status_still_works_in_the_default_in_tree_layout(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC16 CONTROL: the common case is unchanged — no ``REBAR_TRACKER_DIR``, store in tree."""
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    local_id = (rebar_repo / ".tickets-tracker" / ".env-id").read_text().strip()
    _plant_last_pass(rebar_repo, f"local:{local_id}")

    status = rebar.bridge_status(repo_root=str(rebar_repo))

    assert status["verdict"] == "HEALTHY"
    assert status["target_environment_id"] == f"local:{local_id}"


def test_bridge_status_cli_succeeds_against_a_relocated_store(
    rebar_repo: Path, tmp_path: Path
) -> None:
    """E2E through the real CLI entrypoint — the surface the live report came from."""
    store = _relocate(rebar_repo, tmp_path / "outside" / "mcp-tickets")
    local_id = (store / ".env-id").read_text().strip()
    _plant_last_pass(rebar_repo, f"local:{local_id}")

    env = subprocess_env()
    env["REBAR_ROOT"] = str(rebar_repo)
    env["REBAR_TRACKER_DIR"] = str(store)
    env.pop("REBAR_ENV_ID", None)
    completed = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "bridge", "status", "--json"],
        cwd=rebar_repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["verdict"] == "HEALTHY"
