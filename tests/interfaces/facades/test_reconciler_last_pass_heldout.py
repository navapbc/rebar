"""Held-out finalization, ordering, and CAS retry contracts for last-pass."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

LAST_PASS_REF = "refs/reconciler/last-pass"


def _reconciler_main():
    engine_dir = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    return importlib.import_module("rebar_reconciler.__main__")


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


def _remote_payload(remote: Path) -> dict | None:
    oid = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "--verify", "--quiet", LAST_PASS_REF],
        capture_output=True,
        text=True,
        check=False,
    )
    if oid.returncode:
        return None
    raw = subprocess.run(
        ["git", "--git-dir", str(remote), "cat-file", "blob", oid.stdout.strip()],
        capture_output=True,
        check=True,
    ).stdout
    return json.loads(raw)


def _advisory(remote: Path, released: list[dict | None]):
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
        def acquire_pass_lock(_pass_id: str, _repo: Path):
            return None

        @staticmethod
        def release_pass_lock(_pass_id: str, _repo: Path) -> None:
            released.append(_remote_payload(remote))

    return Advisory


def _install_main_stubs(
    reconciler_main,
    monkeypatch: pytest.MonkeyPatch,
    remote: Path,
    released: list[dict | None],
    rc: int,
) -> None:
    advisory = _advisory(remote, released)
    original_load = reconciler_main._load_sibling_keyed

    def load(key: str, name: str):
        return advisory if name == "_advisory_lock.py" else original_load(key, name)

    monkeypatch.setattr(reconciler_main, "_load_sibling_keyed", load)
    monkeypatch.setattr(reconciler_main, "_purge_committed_reconciler_locks", lambda _root: None)
    monkeypatch.setattr(reconciler_main, "run_pass", lambda **_kwargs: rc)
    monkeypatch.setenv("REBAR_ENV_ID", "reconciler")


def test_process_surviving_hard_failure_is_recorded_before_release(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler_main = _reconciler_main()
    remote = _configure_origin(rebar_repo, tmp_path)
    released: list[dict | None] = []
    _install_main_stubs(reconciler_main, monkeypatch, remote, released, 1)

    rc = reconciler_main.main(["sync", "--repo-root", str(rebar_repo)])

    assert rc == 1
    payload = _remote_payload(remote)
    assert payload is not None
    assert payload["outcome"] == "failure"
    assert payload["failure_kind"] == "operational_failure"
    assert released == [payload], "failure witness must be durable before lock release"


def test_writer_without_explicit_environment_uses_stable_local_identity(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler_main = _reconciler_main()
    remote = _configure_origin(rebar_repo, tmp_path)
    released: list[dict | None] = []
    _install_main_stubs(reconciler_main, monkeypatch, remote, released, 0)
    monkeypatch.delenv("REBAR_ENV_ID", raising=False)
    local_id = (rebar_repo / ".tickets-tracker" / ".env-id").read_text().strip()

    rc = reconciler_main.main(["sync", "--repo-root", str(rebar_repo)])

    assert rc == 0
    payload = _remote_payload(remote)
    assert payload is not None
    assert payload["environment_id"] == f"local:{local_id}"
    assert payload["environment_id"] != "reconciler"


def test_last_pass_cas_retries_exactly_three_times_with_jittered_backoff(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler_main = _reconciler_main()
    remote = _configure_origin(rebar_repo, tmp_path)
    released: list[dict | None] = []
    _install_main_stubs(reconciler_main, monkeypatch, remote, released, 0)
    real_run = subprocess.run
    attempts: list[list[str]] = []

    def run(argv, *args, **kwargs):
        words = [str(part) for part in argv]
        if "push" in words and any("refs/reconciler/last-pass" in part for part in words):
            attempts.append(words)
            if len(attempts) < 3:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr="! [rejected] refs/reconciler/last-pass (stale info)",
                )
        return real_run(argv, *args, **kwargs)

    sleeps: list[float] = []
    monkeypatch.setattr(subprocess, "run", run)
    last_pass = importlib.import_module("rebar_reconciler.last_pass")
    monkeypatch.setattr(last_pass, "_sleep", sleeps.append)

    rc = reconciler_main.main(["sync", "--repo-root", str(rebar_repo)])

    assert rc == 0
    assert len(attempts) == 3
    # Bug paediatric-orchestral-anemone: the backoff is now JITTERED, so the two
    # non-final sleeps are the base schedule (0.1, 0.2) plus bounded additive jitter,
    # landing in [base, 1.25*base] — never the raw fixed schedule. The lockstep
    # (non-)collision teeth live in the held-out unit oracle
    # tests/unit/test_last_pass_backoff_jitter_paediatric.py; here we pin the count and
    # the jitter window at the integration tier.
    assert len(sleeps) == 2
    for base, slept in zip((0.1, 0.2), sleeps, strict=True):
        assert base <= slept <= base * 1.25
    # Escalation is preserved: 0.1*1.25 = 0.125 < 0.2, so the second sleep still exceeds
    # the first regardless of the jitter draw.
    assert sleeps[0] < sleeps[1]
    assert _remote_payload(remote)["outcome"] == "success"


def test_persistent_non_cas_push_failure_is_loud_and_still_releases_lock(
    rebar_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reconciler_main = _reconciler_main()
    remote = _configure_origin(rebar_repo, tmp_path)
    released: list[dict | None] = []
    _install_main_stubs(reconciler_main, monkeypatch, remote, released, 0)
    real_run = subprocess.run
    attempts = 0

    def run(argv, *args, **kwargs):
        nonlocal attempts
        words = [str(part) for part in argv]
        if "push" in words and any("refs/reconciler/last-pass" in part for part in words):
            attempts += 1
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="fatal: Authentication failed for origin",
            )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)

    rc = reconciler_main.main(["sync", "--repo-root", str(rebar_repo)])

    assert rc == 1
    assert attempts == 1, "transport/auth failures are not CAS mismatches and must not retry"
    assert released == [None]
    assert "last-pass" in capsys.readouterr().err.lower()
