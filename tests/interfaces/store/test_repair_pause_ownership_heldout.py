"""Held-out failure and boundary oracle for destructive-repair pause ownership."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def _git_repo(path: Path, *, email: str | None = "repair@example.com") -> Path:
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    if email is not None:
        subprocess.run(["git", "-C", str(path), "config", "user.email", email], check=True)
    else:
        # An absent local value would inherit the developer machine's global email.
        # An explicit empty local value exercises identity._git_email's documented
        # empty-output -> None contract deterministically.
        subprocess.run(["git", "-C", str(path), "config", "user.email", ""], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Repair Test"], check=True)
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


def _fsck_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
    mutation,
    *,
    in_flight: bool = False,
):
    from rebar._commands import fsck_repair

    tracker = tmp_path / "tracker"
    tracker.mkdir(exist_ok=True)
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

    monkeypatch.setattr(fsck_repair, "_ticket_dirs", lambda _tracker: ["ticket"])
    monkeypatch.setattr(fsck_repair, "_repair_plan", plan)
    monkeypatch.setattr(fsck_repair, "_repair_ticket", repair_ticket)
    monkeypatch.setattr(fsck_repair, "_git", git)
    monkeypatch.setattr(fsck_repair, "_has_remote", lambda _tracker: False)
    monkeypatch.setattr(fsck_repair, "_resolve_tracker_git_dir", lambda _tracker: None)
    monkeypatch.setattr(fsck_repair, "_reconciler_in_flight", lambda _root=None: in_flight)
    return fsck_repair._repair_run(str(tracker), dry_run=False, repo_root=repo)


def _doctor_run(monkeypatch: pytest.MonkeyPatch, tracker: Path, repo: Path, mutation):
    from rebar._commands import doctor

    findings = [{"kind": "ancestor-blocking"}]
    monkeypatch.setattr(doctor, "_pre_tag", lambda _tracker: "pre-oid")
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda _root=None: False)

    def repair(finding, _tracker, *, repo_root=None):
        mutation()
        finding["repair_status"] = "repaired"

    monkeypatch.setattr(doctor, "repair_finding", repair)
    return doctor.run_repair(findings, str(tracker), repo_root=repo)


@pytest.mark.parametrize("surface", ["fsck", "doctor"])
def test_missing_git_email_aborts_before_pause_or_mutation(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands._seam import CommandError

    repo = _git_repo(tmp_path / "repo", email=None)
    tracker = tmp_path / "doctor-tracker"
    tracker.mkdir()
    ref_lock = _ref_lock()
    mutated = False

    def mutation() -> None:
        nonlocal mutated
        mutated = True

    if surface == "fsck":
        lines, unresolved = _fsck_run(monkeypatch, tmp_path, repo, mutation)
        assert unresolved == -1
        assert "Error: fsck repair requires a configured git user.email" in "\n".join(lines)
    else:
        with pytest.raises(
            CommandError, match=r"Error: doctor repair requires a configured git user\.email"
        ):
            _doctor_run(monkeypatch, tracker, repo, mutation)

    assert mutated is False
    assert ref_lock.read_pause(repo) is None


@pytest.mark.parametrize("surface", ["fsck", "doctor"])
def test_foreign_pause_aborts_without_clearing_or_mutating(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands._seam import CommandError

    repo = _git_repo(tmp_path / "repo")
    tracker = tmp_path / "doctor-tracker"
    tracker.mkdir()
    ref_lock = _ref_lock()
    foreign_oid = ref_lock.set_pause(
        repo,
        reason="database cutover",
        who="other@example.com",
        paused_at="2026-08-09T18:00:00Z",
    )
    mutated = False

    def mutation() -> None:
        nonlocal mutated
        mutated = True

    if surface == "fsck":
        lines, unresolved = _fsck_run(monkeypatch, tmp_path, repo, mutation)
        assert unresolved == -1, lines
    else:
        with pytest.raises(CommandError, match="database cutover"):
            _doctor_run(monkeypatch, tracker, repo, mutation)

    assert mutated is False
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", ref_lock.GATE_REF],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == foreign_oid
    )
    assert ref_lock.read_pause(repo)["reason"] == "database cutover"


@pytest.mark.parametrize("surface", ["fsck", "doctor"])
def test_pause_creation_uncertainty_fails_closed_before_mutation(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands._seam import CommandError

    repo = _git_repo(tmp_path / "repo")
    tracker = tmp_path / "doctor-tracker"
    tracker.mkdir()
    ref_lock = _ref_lock()
    mutated = False

    def uncertain(*_args, **_kwargs):
        raise ref_lock.RefLockTimeoutError("timed out creating repair pause")

    def mutation() -> None:
        nonlocal mutated
        mutated = True

    monkeypatch.setattr(ref_lock, "set_pause", uncertain)
    if surface == "fsck":
        lines, unresolved = _fsck_run(monkeypatch, tmp_path, repo, mutation)
        assert unresolved == -1
        assert "could not acquire its reconciliation pause" in "\n".join(lines)
    else:
        with pytest.raises(CommandError, match="could not acquire its reconciliation pause"):
            _doctor_run(monkeypatch, tracker, repo, mutation)
    assert mutated is False


@pytest.mark.parametrize("surface", ["fsck", "doctor"])
def test_replaced_pause_is_surfaced_and_never_cleared(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands._seam import CommandError

    repo = _git_repo(tmp_path / "repo")
    tracker = tmp_path / "doctor-tracker"
    tracker.mkdir()
    ref_lock = _ref_lock()

    def replace_owned_pause() -> None:
        owned = ref_lock.read_pause_with_oid(repo)
        assert owned is not None
        _pause, owned_oid = owned
        assert ref_lock.release(repo, ref_lock.GATE_REF, oid=owned_oid)
        ref_lock.set_pause(
            repo,
            reason="operator took over",
            who="other@example.com",
            paused_at="2026-08-09T19:00:00Z",
        )

    if surface == "fsck":
        lines, unresolved = _fsck_run(monkeypatch, tmp_path, repo, replace_owned_pause)
        assert unresolved == -1, lines
    else:
        with pytest.raises(CommandError, match="pause replaced during cleanup"):
            _doctor_run(monkeypatch, tracker, repo, replace_owned_pause)

    assert ref_lock.read_pause(repo)["reason"] == "operator took over"


@pytest.mark.parametrize("surface", ["fsck", "doctor"])
def test_same_reason_with_a_new_oid_is_still_foreign(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands._seam import CommandError

    repo = _git_repo(tmp_path / "repo")
    tracker = tmp_path / "doctor-tracker"
    tracker.mkdir()
    ref_lock = _ref_lock()

    def replace_owned_pause() -> None:
        owned = ref_lock.read_pause_with_oid(repo)
        assert owned is not None
        pause, owned_oid = owned
        assert ref_lock.release(repo, ref_lock.GATE_REF, oid=owned_oid)
        ref_lock.set_pause(
            repo,
            reason=str(pause["reason"]),
            who="other@example.com",
            paused_at="2026-08-09T19:00:00Z",
        )

    if surface == "fsck":
        lines, unresolved = _fsck_run(monkeypatch, tmp_path, repo, replace_owned_pause)
        assert unresolved == -1, lines
    else:
        with pytest.raises(CommandError, match="pause replaced during cleanup"):
            _doctor_run(monkeypatch, tracker, repo, replace_owned_pause)

    remaining = ref_lock.read_pause(repo)
    assert remaining is not None
    assert remaining["who"] == "other@example.com"


@pytest.mark.parametrize("surface", ["fsck", "doctor"])
def test_missing_pause_at_cleanup_is_an_error_and_is_not_recreated(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands._seam import CommandError

    repo = _git_repo(tmp_path / "repo")
    tracker = tmp_path / "doctor-tracker"
    tracker.mkdir()
    ref_lock = _ref_lock()

    def remove_owned_pause() -> None:
        owned = ref_lock.read_pause_with_oid(repo)
        assert owned is not None
        _pause, owned_oid = owned
        assert ref_lock.release(repo, ref_lock.GATE_REF, oid=owned_oid)

    if surface == "fsck":
        lines, unresolved = _fsck_run(monkeypatch, tmp_path, repo, remove_owned_pause)
        assert unresolved == -1, lines
    else:
        with pytest.raises(CommandError, match="lost its reconciliation pause"):
            _doctor_run(monkeypatch, tracker, repo, remove_owned_pause)

    assert ref_lock.read_pause(repo) is None


@pytest.mark.parametrize("surface", ["fsck", "doctor"])
def test_cleanup_cas_miss_is_surfaced_without_retry(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands._seam import CommandError

    repo = _git_repo(tmp_path / "repo")
    tracker = tmp_path / "doctor-tracker"
    tracker.mkdir()
    ref_lock = _ref_lock()
    releases = 0

    def lose_once(*_args, **_kwargs) -> bool:
        nonlocal releases
        releases += 1
        return False

    monkeypatch.setattr(ref_lock, "release", lose_once)
    if surface == "fsck":
        lines, unresolved = _fsck_run(monkeypatch, tmp_path, repo, lambda: None)
        assert unresolved == -1, lines
    else:
        with pytest.raises(CommandError, match="lost the cleanup CAS"):
            _doctor_run(monkeypatch, tracker, repo, lambda: None)

    assert releases == 1
    pause = ref_lock.read_pause(repo)
    assert pause is not None
    assert str(pause["reason"]).startswith(f"repair:{surface}:")


@pytest.mark.parametrize(
    "environment",
    [{}, {"GITHUB_ACTIONS": "true"}, {"JENKINS_URL": "https://jenkins/"}, {"GITLAB_CI": "true"}],
)
def test_provider_shaped_fsck_invocations_share_the_same_pause_contract(
    environment: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "repo")
    ref_lock = _ref_lock()
    seen = False
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    def mutation() -> None:
        nonlocal seen
        pause = ref_lock.read_pause(repo)
        assert pause is not None
        assert str(pause["reason"]).startswith("repair:fsck:")
        seen = True

    lines, unresolved = _fsck_run(monkeypatch, tmp_path, repo, mutation)
    assert unresolved == 0, lines
    assert seen
    assert ref_lock.read_pause(repo) is None
