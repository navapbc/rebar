"""Happy-path contract for provider-neutral destructive-repair pause ownership."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "repair@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Repair Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return path


def _ref_lock():
    from rebar._engine import engine_dir

    engine = str(engine_dir())
    if engine not in sys.path:
        sys.path.insert(0, engine)
    from rebar_reconciler import _advisory_lock as advisory

    return advisory._load_ref_lock()


def _result(args, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def _stub_fsck_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation) -> Path:
    from rebar._commands import fsck_repair

    tracker = tmp_path / "tracker"
    tracker.mkdir()
    repaired = False

    def plan(*_args):
        return {
            "retire": [] if repaired else ["source"],
            "auto_orphans": [],
            "triage_orphans": [],
            "stale_channel": [],
        }

    def repair_ticket(*_args, **_kwargs):
        nonlocal repaired
        mutation()
        repaired = True
        return {}

    def git(_tracker: str, *args: str):
        if args == ("rev-parse", "HEAD"):
            return _result(args, stdout=f"{'a' * 40}\n")
        return _result(args)

    monkeypatch.setattr(fsck_repair, "_ticket_dirs", lambda _tracker, **_kw: ["ticket"])
    monkeypatch.setattr(fsck_repair, "_repair_plan", plan)
    monkeypatch.setattr(fsck_repair, "_repair_ticket", repair_ticket)
    monkeypatch.setattr(fsck_repair, "_git", git)
    monkeypatch.setattr(fsck_repair, "_has_remote", lambda _tracker: False)
    monkeypatch.setattr(fsck_repair, "_resolve_tracker_git_dir", lambda _tracker: None)
    monkeypatch.setattr(fsck_repair, "_reconciler_in_flight", lambda _root=None: False)
    return tracker


@pytest.mark.unit
@pytest.mark.scripts
def test_fsck_repair_owns_pause_during_mutation_and_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands import fsck_repair

    repo = _git_repo(tmp_path / "repo")
    ref_lock = _ref_lock()
    observed: list[dict[str, object]] = []

    def mutation() -> None:
        pause = ref_lock.read_pause(repo)
        assert pause is not None
        assert str(pause["reason"]).startswith("repair:fsck:")
        assert pause["who"] == "repair@example.com"
        observed.append(pause)

    tracker = _stub_fsck_run(monkeypatch, tmp_path, mutation)
    gh_calls: list[list[str]] = []
    real_run = subprocess.run

    def reject_gh(args, *pargs, **kwargs):
        if args and args[0] == "gh":
            gh_calls.append(args)
            return _result(args, returncode=127, stderr="gh unavailable")
        return real_run(args, *pargs, **kwargs)

    monkeypatch.setattr(fsck_repair.subprocess, "run", reject_gh)

    lines, unresolved = fsck_repair._repair_run(str(tracker), dry_run=False, repo_root=repo)

    assert unresolved == 0, lines
    assert observed
    assert ref_lock.read_pause(repo) is None
    assert gh_calls == []


@pytest.mark.unit
@pytest.mark.scripts
def test_doctor_repair_owns_same_pause_contract_and_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands import doctor

    repo = _git_repo(tmp_path / "repo")
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    ref_lock = _ref_lock()
    findings = [{"kind": "ancestor-blocking"}]
    observed: list[dict[str, object]] = []

    monkeypatch.setattr(doctor, "_pre_tag", lambda _tracker: "pre-oid")
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda _root=None: False)

    def repair(finding, _tracker, *, repo_root=None):
        pause = ref_lock.read_pause(repo)
        assert pause is not None
        assert str(pause["reason"]).startswith("repair:doctor:")
        assert pause["who"] == "repair@example.com"
        observed.append(pause)
        finding["repair_status"] = "repaired"

    monkeypatch.setattr(doctor, "repair_finding", repair)

    repaired, pre_oid = doctor.run_repair(findings, str(tracker), repo_root=repo)

    assert repaired == findings
    assert pre_oid == "pre-oid"
    assert observed
    assert ref_lock.read_pause(repo) is None
