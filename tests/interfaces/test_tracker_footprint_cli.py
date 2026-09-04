"""Real-CLI contracts for opt-in, read-only tracker-footprint measurement."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from _subprocess_env import subprocess_env

from rebar import schemas


def _run_git(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *argv], check=check, capture_output=True, text=True)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git in a NON-BARE worktree, which git may discover from ``repo``."""
    return _run_git("-C", str(repo), *args, check=check)


def _bare_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git against a BARE repository, which must be named explicitly.

    git refuses to *discover* a bare repository through ``-C`` when a developer
    sets ``safe.bareRepository = explicit`` [rebar:740d-187c-53a2-4b7d].
    """
    return _run_git("--git-dir", str(repo), *args, check=check)


def _run_cli(
    repo: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = subprocess_env()
    env["REBAR_ROOT"] = str(repo)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _publish_ticket_store(repo: Path, remote: Path, *, small_files: int = 0) -> Path:
    _git(remote.parent, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    tracker = repo / ".tickets-tracker"
    if small_files:
        bulk = tracker / "footprint-small-files"
        bulk.mkdir()
        for index in range(small_files):
            (bulk / f"{index:04d}").write_bytes(b"x")
        _git(tracker, "add", "footprint-small-files")
        _git(tracker, "commit", "-q", "-m", "seed small files")
    _git(tracker, "push", "origin", "tickets:refs/heads/tickets")
    return tracker


def _availability_value(field: object) -> int:
    assert isinstance(field, dict)
    assert set(field) == {"value"}
    value = field["value"]
    assert isinstance(value, int)
    return value


def _repo_state(repo: Path) -> tuple[str, str, str]:
    return (
        _git(repo, "rev-parse", "HEAD").stdout,
        _git(repo, "show-ref").stdout,
        _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout,
    )


def test_fresh_clone_json_exposes_allocation_without_a_policy_verdict(
    rebar_repo: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "origin.git"
    tracker = _publish_ticket_store(rebar_repo, remote, small_files=256)
    before_project = _repo_state(rebar_repo)
    before_tracker = _repo_state(tracker)
    before_remote_refs = _bare_git(remote, "show-ref").stdout

    completed = _run_cli(
        rebar_repo,
        "tracker-footprint",
        "--fresh-clone",
        "--output",
        "json",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    schemas.validator(schemas.TRACKER_FOOTPRINT).validate(payload)
    assert payload["mode"] == "fresh-clone"
    assert payload["source"]["requested_ref"] == "origin/tickets"
    assert payload["object_database"] == {
        "scope": "standalone",
        "shared_reasons": [],
    }
    checkout_allocated = _availability_value(payload["layers"]["checkout"]["allocated_bytes"])
    pack_logical = payload["layers"]["pack"]["logical_bytes"]
    # A fresh single-branch clone owns its objects outright: the pack measurement is complete.
    assert payload["layers"]["pack"]["complete"] is True
    assert pack_logical > 0
    # Capability assertion: this platform exposes st_blocks, so allocation is a concrete value.
    # We deliberately avoid asserting any allocated-to-pack ratio, which would depend on the
    # host filesystem's block-charging policy (see test_footprint injected-block coverage).
    assert checkout_allocated >= 0
    assert payload["layers"]["checkout"]["file_count"] >= 256
    serialized = json.dumps(payload).lower()
    assert "threshold" not in serialized
    assert "verdict" not in serialized
    assert _repo_state(rebar_repo) == before_project
    assert _repo_state(tracker) == before_tracker
    assert _bare_git(remote, "show-ref").stdout == before_remote_refs


def test_mounted_linked_store_is_shared_and_read_only(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"
    before_project = _repo_state(rebar_repo)
    before_tracker = _repo_state(tracker)

    completed = _run_cli(rebar_repo, "tracker-footprint", "--output", "json")

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    schemas.validator(schemas.TRACKER_FOOTPRINT).validate(payload)
    assert payload["mode"] == "mounted"
    assert payload["object_database"] == {
        "scope": "shared",
        "shared_reasons": ["linked-worktree"],
    }
    assert payload["layers"]["whole_clone"]["scope"] == "shared"
    assert _repo_state(rebar_repo) == before_project
    assert _repo_state(tracker) == before_tracker


def test_text_output_names_each_layer_and_requested_ref(rebar_repo: Path) -> None:
    completed = _run_cli(rebar_repo, "tracker-footprint")

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    normalized = completed.stdout.lower().replace("_", " ")
    for label in (
        "origin/tickets",
        "object database",
        "pack",
        "checkout",
        "git directory",
        "whole clone",
    ):
        assert label in normalized


def test_invalid_configured_branch_cleans_only_its_temporary_child(
    rebar_repo: Path, tmp_path: Path
) -> None:
    secret = "credential-sentinel-do-not-leak"
    remote = tmp_path / f"{secret}.git"
    _publish_ticket_store(rebar_repo, remote)
    before_remote_refs = _bare_git(remote, "show-ref").stdout
    temp_root = tmp_path / "command-tmp"
    temp_root.mkdir()
    sentinel = temp_root / "keep-me"
    sentinel.write_text("operator-owned\n", encoding="utf-8")

    completed = _run_cli(
        rebar_repo,
        "tracker-footprint",
        "--fresh-clone",
        extra_env={
            "REBAR_TRACKER_BRANCH": "missing-branch",
            "TMPDIR": str(temp_root),
        },
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "origin/missing-branch" in completed.stderr
    assert secret not in completed.stderr
    assert secret not in completed.stdout
    assert sentinel.read_text(encoding="utf-8") == "operator-owned\n"
    assert set(os.listdir(temp_root)) == {sentinel.name}
    assert _bare_git(remote, "show-ref").stdout == before_remote_refs


def test_unknown_argument_exits_two_without_a_report(rebar_repo: Path) -> None:
    completed = _run_cli(rebar_repo, "tracker-footprint", "--nonexistent-flag")

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "error:" in completed.stderr
    assert "usage:" in completed.stderr.lower()
