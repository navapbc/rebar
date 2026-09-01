"""Remote-anchored eligibility predicates for ADR 0106 reclamation targets."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

from rebar._store import reclaim_eligibility

pytestmark = pytest.mark.integration

OLD_DATE = "2024-01-01T00:00:00+00:00"
RECENT_DATE = "2024-02-10T00:00:00+00:00"
NOW = datetime(2024, 2, 15, tzinfo=UTC)


@dataclass(frozen=True)
class RemoteFixture:
    repo: Path
    remote: Path


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _dated_env(date: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    return env


@pytest.fixture
def remote_fixture(tmp_path: Path) -> RemoteFixture:
    repo = tmp_path / "repo"
    remote = tmp_path / "origin.git"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Reclaim Eligibility")
    _git(repo, "config", "user.email", "reclaim-eligibility@example.com")
    init_bare_remote(remote)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "switch", "-q", "-c", "tickets")
    (repo / "rebar.toml").write_text("[reclaim]\nhorizon_days = 30\n", encoding="utf-8")
    (repo / "base.json").write_text('{"base": true}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base", env=_dated_env(OLD_DATE))
    _git(repo, "push", "-q", "origin", "tickets:tickets")
    _git(repo, "update-ref", "refs/remotes/origin/tickets", "refs/heads/tickets")
    return RemoteFixture(repo=repo, remote=remote)


def _commit(repo: Path, relative_path: str, body: str, *, date: str, message: str) -> str:
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, env=_dated_env(date))
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _publish_and_track(repo: Path) -> None:
    _git(repo, "push", "-q", "origin", "tickets:tickets")
    _git(repo, "update-ref", "refs/remotes/origin/tickets", "refs/heads/tickets")


@pytest.mark.parametrize(
    ("commit_date", "published", "expected"),
    [
        (OLD_DATE, True, True),
        (RECENT_DATE, True, False),
        (OLD_DATE, False, False),
    ],
)
def test_checkpoint_commit_eligibility_uses_remote_reachability_and_horizon(
    remote_fixture: RemoteFixture, commit_date: str, published: bool, expected: bool
) -> None:
    checkpoint = _commit(
        remote_fixture.repo,
        "checkpoint.json",
        '{"checkpoint": true}\n',
        date=commit_date,
        message="checkpoint",
    )
    if published:
        _publish_and_track(remote_fixture.repo)

    assert (
        reclaim_eligibility.checkpoint_commit_eligible(remote_fixture.repo, checkpoint, now=NOW)
        is expected
    )


@pytest.mark.parametrize(
    ("commit_date", "published", "expected"),
    [
        (OLD_DATE, True, True),
        (RECENT_DATE, True, False),
        (OLD_DATE, False, False),
    ],
)
def test_retired_tombstone_eligibility_uses_folding_commit(
    remote_fixture: RemoteFixture, commit_date: str, published: bool, expected: bool
) -> None:
    tombstone = "ticket/0001-COMMENT.json.retired"
    _commit(
        remote_fixture.repo,
        tombstone,
        '{"event_type": "COMMENT"}\n',
        date=commit_date,
        message="retire source event",
    )
    if published:
        _publish_and_track(remote_fixture.repo)

    assert (
        reclaim_eligibility.retired_tombstone_eligible(remote_fixture.repo, tombstone, now=NOW)
        is expected
    )


def test_reclaim_horizon_days_config_changes_eligibility(remote_fixture: RemoteFixture) -> None:
    checkpoint = _commit(
        remote_fixture.repo,
        "checkpoint.json",
        '{"checkpoint": true}\n',
        date="2024-02-01T00:00:00+00:00",
        message="checkpoint",
    )
    _publish_and_track(remote_fixture.repo)

    assert not reclaim_eligibility.checkpoint_commit_eligible(
        remote_fixture.repo, checkpoint, now=NOW
    )

    (remote_fixture.repo / "rebar.toml").write_text(
        "[reclaim]\nhorizon_days = 10\n", encoding="utf-8"
    )

    assert reclaim_eligibility.checkpoint_commit_eligible(remote_fixture.repo, checkpoint, now=NOW)
