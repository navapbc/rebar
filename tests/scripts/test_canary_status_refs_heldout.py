"""Held-out canary E2E over real reconciler refs and the real status CLI."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _subprocess_env import subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canary_bridge.py"


@pytest.fixture(scope="module")
def canary() -> ModuleType:
    spec = importlib.util.spec_from_file_location("canary_status_refs_heldout", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    tracker = repo / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("local-test\n")
    return repo


def _plant(repo: Path, ref: str, payload: dict) -> str:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    oid = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=raw,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(["git", "update-ref", ref, oid], cwd=repo, check=True)
    return oid


class RealStatusRunner:
    def __init__(self, repo: Path, *, advance: bool):
        self.repo = repo
        self.advance = advance
        self.status_calls = 0

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        assert argv[:3] == ["rebar", "bridge", "status"]
        self.status_calls += 1
        if self.advance and self.status_calls == 2:
            _plant(
                self.repo,
                "refs/reconciler/lock",
                {
                    "holder": "pass-live",
                    "lease_secs": 120,
                    "heartbeat_ns": 456,
                    "fence": 10,
                },
            )
        env = subprocess_env()
        env["REBAR_ROOT"] = str(self.repo)
        completed = subprocess.run(
            [sys.executable, "-m", "rebar.cli", *argv[1:]],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr


@pytest.mark.parametrize(
    ("advance", "stale", "word"),
    [(True, "false", "advanced"), (False, "true", "crashed")],
)
def test_real_ref_fence_progress_classifies_running_or_crashed(
    canary: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    advance: bool,
    stale: str,
    word: str,
) -> None:
    repo = _repo(tmp_path)
    _plant(
        repo,
        "refs/reconciler/last-pass",
        {
            "schema_version": 1,
            "pass_id": "pass-prior",
            "environment_id": "reconciler",
            "outcome": "success",
            "completed_at": "2026-08-09T12:00:00Z",
            "lock_fence": 8,
        },
    )
    _plant(
        repo,
        "refs/reconciler/lock",
        {
            "holder": "pass-live",
            "lease_secs": 120,
            "heartbeat_ns": 123,
            "fence": 9,
        },
    )
    monkeypatch.setattr(canary.time, "sleep", lambda _seconds: None)
    output = tmp_path / "output"
    output.touch()
    env = {
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "navapbc/rebar",
        "ALERT_WINDOW_HOURS": "2",
        "REBAR_CANARY_HEARTBEAT_SOURCE": "status",
    }

    rc = canary.main(
        ["check-heartbeat"],
        runner=RealStatusRunner(repo, advance=advance),
        environ=env,
        now_epoch=1_786_300_000,
    )

    assert rc == 0
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["stale"] == stale
    assert word in outputs["status_msg"].lower()
