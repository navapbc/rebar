"""Held-out edge oracle for ``bridge status`` and its compatibility alias."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

LAST_PASS_REF = "refs/reconciler/last-pass"
LOCK_REF = "refs/reconciler/lock"
GATE_REF = "refs/reconciler/gate"


def _run_cli(
    repo: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = subprocess_env()
    env["REBAR_ROOT"] = str(repo)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _plant_blob(repo: Path, ref: str, payload: dict) -> str:
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
        ["git", "-C", str(repo), "update-ref", ref, oid], check=True, capture_output=True
    )
    return oid


def _delete_ref(repo: Path, ref: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "-d", ref], check=True, capture_output=True
    )


def _record(
    repo: Path,
    *,
    environment_id: str,
    outcome: str = "success",
    failure_kind: str | None = None,
    completed_at: str = "2020-01-01T00:00:00Z",
) -> dict:
    payload = {
        "schema_version": 1,
        "pass_id": "pass-edge",
        "environment_id": environment_id,
        "outcome": outcome,
        "completed_at": completed_at,
        "lock_fence": 11,
    }
    if failure_kind is not None:
        payload["failure_kind"] = failure_kind
    _plant_blob(repo, LAST_PASS_REF, payload)
    return payload


def _json_status(repo: Path, *args: str, extra_env: dict[str, str] | None = None):
    completed = _run_cli(repo, "bridge", "status", "--json", *args, extra_env=extra_env)
    data = json.loads(completed.stdout) if completed.stdout else None
    return completed, data


def _refs(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show-ref"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def test_canonical_and_legacy_status_are_byte_identical_and_read_only(
    rebar_repo: Path,
) -> None:
    env_id = (rebar_repo / ".tickets-tracker" / ".env-id").read_text().strip()
    _record(rebar_repo, environment_id=f"local:{env_id}")
    before_refs = _refs(rebar_repo)
    before_tracker = {
        str(p.relative_to(rebar_repo / ".tickets-tracker")): p.read_bytes()
        for p in (rebar_repo / ".tickets-tracker").rglob("*")
        if p.is_file() and ".git" not in p.parts
    }

    canonical = _run_cli(rebar_repo, "bridge", "status", "--json")
    legacy = _run_cli(rebar_repo, "bridge-status", "--json")

    assert (legacy.returncode, legacy.stdout, legacy.stderr) == (
        canonical.returncode,
        canonical.stdout,
        canonical.stderr,
    )
    assert _refs(rebar_repo) == before_refs
    after_tracker = {
        str(p.relative_to(rebar_repo / ".tickets-tracker")): p.read_bytes()
        for p in (rebar_repo / ".tickets-tracker").rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    assert after_tracker == before_tracker


def test_status_help_is_canonical_while_legacy_stays_hidden(rebar_repo: Path) -> None:
    nested = _run_cli(rebar_repo, "bridge", "--help")
    overview = _run_cli(rebar_repo, "--help")
    legacy = _run_cli(rebar_repo, "bridge-status", "--help")
    assert nested.returncode == 0 and "status" in nested.stdout
    assert "bridge-status" not in overview.stdout
    assert legacy.returncode == 0
    assert "usage: rebar bridge status" in legacy.stdout.lower()


def test_omitted_max_age_disables_age_failure_and_explicit_age_is_stale(
    rebar_repo: Path,
) -> None:
    env_id = (rebar_repo / ".tickets-tracker" / ".env-id").read_text().strip()
    _record(rebar_repo, environment_id=f"local:{env_id}")
    no_limit, healthy = _json_status(rebar_repo)
    limited, stale = _json_status(rebar_repo, "--max-age", "1")
    assert no_limit.returncode == 0 and healthy["verdict"] == "HEALTHY"
    assert limited.returncode == 1 and stale["verdict"] == "STALE"


def test_paused_dominates_age_and_failure(rebar_repo: Path) -> None:
    _record(
        rebar_repo,
        environment_id="reconciler",
        outcome="failure",
        failure_kind="apply_error",
    )
    _plant_blob(
        rebar_repo,
        GATE_REF,
        {
            "gated_mode": "reconcile-check",
            "paused": True,
            "reason": "maintenance",
            "who": "ops@example.com",
            "paused_at": "2026-08-09T12:00:00Z",
        },
    )
    completed, status = _json_status(rebar_repo, "--target", "reconciler", "--max-age", "1")
    assert completed.returncode == 0
    assert status["verdict"] == "PAUSED"
    assert status["failure_kind"] == "apply_error"
    assert status["pause"]["reason"] == "maintenance"


def test_running_uses_live_lock_and_dominates_foreign_failure_and_stale(
    rebar_repo: Path,
) -> None:
    _record(
        rebar_repo,
        environment_id="somewhere-else",
        outcome="failure",
        failure_kind="apply_error",
    )
    oid = _plant_blob(
        rebar_repo,
        LOCK_REF,
        {"holder": "pass-live", "lease_secs": 120, "heartbeat_ns": 123, "fence": 9},
    )
    completed, status = _json_status(rebar_repo, "--target", "reconciler", "--max-age", "1")
    assert completed.returncode == 0
    assert status["verdict"] == "RUNNING"
    assert status["lock"] == {
        "oid": oid,
        "holder": "pass-live",
        "lease_secs": 120,
        "heartbeat_ns": 123,
        "fence": 9,
    }
    assert status["environment_id"] == "somewhere-else"
    assert status["failure_kind"] == "apply_error"


@pytest.mark.parametrize(
    ("environment_id", "outcome", "failure_kind", "args", "verdict"),
    [
        ("foreign", "success", None, ("--target", "reconciler"), "FOREIGN"),
        ("reconciler", "failure", "reschedule", ("--target", "reconciler"), "FAILED"),
    ],
)
def test_failure_verdicts(
    rebar_repo: Path,
    environment_id: str,
    outcome: str,
    failure_kind: str | None,
    args: tuple[str, ...],
    verdict: str,
) -> None:
    _record(
        rebar_repo,
        environment_id=environment_id,
        outcome=outcome,
        failure_kind=failure_kind,
    )
    completed, status = _json_status(rebar_repo, *args)
    assert completed.returncode == 1
    assert status["verdict"] == verdict


def test_never_run_requires_no_record_and_no_lock(rebar_repo: Path) -> None:
    _delete_ref(rebar_repo, LAST_PASS_REF)
    _delete_ref(rebar_repo, LOCK_REF)
    completed, status = _json_status(rebar_repo, "--target", "reconciler")
    assert completed.returncode == 1
    assert status["verdict"] == "NEVER_RUN"


@pytest.mark.parametrize("contents", [None, "", "  \n"])
def test_local_identity_requires_nonempty_env_id(rebar_repo: Path, contents: str | None) -> None:
    env_path = rebar_repo / ".tickets-tracker" / ".env-id"
    if contents is None:
        env_path.unlink()
    else:
        env_path.write_text(contents)
    completed = _run_cli(rebar_repo, "bridge", "status", "--json")
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert ".env-id" in completed.stderr
    assert "reconciler" not in completed.stderr.lower()


def test_explicit_environment_overrides_rebar_env_id(rebar_repo: Path) -> None:
    _record(rebar_repo, environment_id="explicit")
    completed, status = _json_status(
        rebar_repo,
        "--target",
        "explicit",
        extra_env={"REBAR_ENV_ID": "ambient"},
    )
    assert completed.returncode == 0
    assert status["verdict"] == "HEALTHY"
    assert status["target_environment_id"] == "explicit"


def test_matching_rich_detail_is_used_and_mismatch_is_ignored(rebar_repo: Path) -> None:
    _record(rebar_repo, environment_id="reconciler")
    detail_path = rebar_repo / ".tickets-tracker" / ".bridge_state" / "last-pass.json"
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    detail_path.write_text(
        json.dumps(
            {
                "pass_id": "pass-edge",
                "environment_id": "reconciler",
                "mutation_count": 4,
            }
        )
    )
    matched, good = _json_status(rebar_repo, "--target", "reconciler")
    assert matched.returncode == 0
    assert good["detail_status"] == "matching"
    assert good["detail"]["mutation_count"] == 4

    detail_path.write_text(json.dumps({"pass_id": "another-pass", "environment_id": "reconciler"}))
    mismatched, stale = _json_status(rebar_repo, "--target", "reconciler")
    assert mismatched.returncode == 0
    assert stale["detail_status"] == "mismatched"
    assert "detail" not in stale
