"""Happy-path contract for the durable reconciler status witness."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

LAST_PASS_REF = "refs/reconciler/last-pass"


def _reconciler_main():
    engine_dir = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    return importlib.import_module("rebar_reconciler.__main__")


def _run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = subprocess_env()
    env["REBAR_ROOT"] = str(repo)
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
        ["git", "-C", str(repo), "update-ref", ref, oid],
        capture_output=True,
        check=True,
    )
    return oid


def _configure_origin(repo: Path, tmp_path: Path) -> Path:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "HEAD"],
        check=True,
        capture_output=True,
    )
    return remote


def _ref_payload(git_dir: Path, ref: str) -> dict | None:
    oid = subprocess.run(
        ["git", "--git-dir", str(git_dir), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if oid.returncode:
        return None
    raw = subprocess.run(
        ["git", "--git-dir", str(git_dir), "cat-file", "blob", oid.stdout.strip()],
        capture_output=True,
        check=True,
    ).stdout
    return json.loads(raw)


def test_matching_durable_success_is_healthy_on_real_cli(rebar_repo: Path) -> None:
    """A real ref record is consumed through the real canonical CLI entrypoint."""
    local_id = (rebar_repo / ".tickets-tracker" / ".env-id").read_text().strip()
    _plant_blob(
        rebar_repo,
        LAST_PASS_REF,
        {
            "schema_version": 1,
            "pass_id": "pass-happy",
            "environment_id": f"local:{local_id}",
            "outcome": "success",
            "completed_at": "2026-08-09T12:00:00Z",
            "lock_fence": 7,
        },
    )

    completed = _run_cli(rebar_repo, "bridge", "status", "--json")

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    status = json.loads(completed.stdout)
    assert status["verdict"] == "HEALTHY"
    assert status["pass_id"] == "pass-happy"
    assert status["environment_id"] == f"local:{local_id}"
    assert status["target_environment_id"] == f"local:{local_id}"
    assert status["outcome"] == "success"
    assert status["lock_fence"] == 7


def test_mutating_reconciler_records_success_before_releasing_lock(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Process finalization publishes the durable witness while the pass lock is held."""
    reconciler_main = _reconciler_main()
    remote = _configure_origin(rebar_repo, tmp_path)
    observed: list[str] = []

    class Advisory:
        class ReconcileLockError(RuntimeError):
            pass

        class ReconcileGateError(RuntimeError):
            pass

        @staticmethod
        def read_pause(_repo: Path):
            return None

        @staticmethod
        def check_pass_lock(_repo: Path) -> bool:
            return False

        @staticmethod
        def check_phase_gate(_mode, _repo: Path) -> bool:
            return False

        @staticmethod
        def acquire_pass_lock(pass_id: str, _repo: Path):
            observed.append(f"acquire:{pass_id}")
            return None

        @staticmethod
        def release_pass_lock(pass_id: str, _repo: Path) -> None:
            payload = _ref_payload(remote, LAST_PASS_REF)
            assert payload is not None, "last-pass must be durable before lock release"
            observed.append(f"release:{pass_id}:{payload['outcome']}")

    original_load = reconciler_main._load_sibling_keyed

    def load(key: str, name: str):
        if name == "_advisory_lock.py":
            return Advisory
        return original_load(key, name)

    monkeypatch.setattr(reconciler_main, "_load_sibling_keyed", load)
    monkeypatch.setattr(reconciler_main, "_purge_committed_reconciler_locks", lambda _root: None)
    monkeypatch.setattr(reconciler_main, "run_pass", lambda **_kwargs: 0)
    monkeypatch.setenv("REBAR_ENV_ID", "reconciler")

    rc = reconciler_main.main(["sync", "--repo-root", str(rebar_repo)])

    assert rc == 0
    payload = _ref_payload(remote, LAST_PASS_REF)
    assert payload is not None
    assert payload["schema_version"] == 1
    assert payload["environment_id"] == "reconciler"
    assert payload["outcome"] == "success"
    assert payload["failure_kind"] is None
    assert observed[0].startswith("acquire:")
    assert observed[-1].endswith(":success")
