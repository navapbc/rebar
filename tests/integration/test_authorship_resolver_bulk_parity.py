"""Real-store parity between B5's point resolvers and the bulk map builders."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rebar.attest import authorship

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_point_resolvers_match_bulk_maps_for_one_hundred_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _git(tracker, "init", "--quiet", "--initial-branch=tickets")
    _git(tracker, "config", "user.email", "ci@example.com")
    _git(tracker, "config", "user.name", "CI fixture")
    _git(tracker, "config", "commit.gpgsign", "false")

    samples: list[tuple[str, Path, str]] = []
    for batch in range(5):
        for offset in range(20):
            index = batch * 20 + offset
            ticket_dir = tracker / f"{index % 4:032x}"
            ticket_dir.mkdir(exist_ok=True)
            position = f"{1_700_000_000_000_000_000 + index}-{uuid.UUID(int=index + 1)}"
            event = ticket_dir / f"{position}-CREATE.json"
            event.write_text(f'{{"index":{index}}}\n', encoding="utf-8")
            samples.append((position, ticket_dir, event.relative_to(tracker).as_posix()))
        _git(tracker, "add", "-A")
        _git(tracker, "commit", "--quiet", "-m", f"event batch {batch}")

    from rebar._commands import _seam

    monkeypatch.setattr(_seam, "tracker_dir", lambda repo_root=None: tracker)
    real_run = subprocess.run
    calls: list[list[str]] = []

    def recording_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(authorship, "subprocess", SimpleNamespace(run=recording_run))
    introducing = authorship.build_introducing_commit_map(repo_root=str(tracker))
    positions = authorship.build_position_commit_map(repo_root=str(tracker))
    assert len(samples) == 100
    for position, ticket_dir, relative_path in samples:
        expected = introducing[relative_path]
        assert authorship.resolve_event_commit(position, str(ticket_dir)) == expected
        assert authorship.resolve_position_commit(position, str(tracker)) == positions[position]
        assert positions[position] == expected
    point_calls = [argv for argv in calls if "--format=%H" in argv]
    assert len(point_calls) == 200
    for argv in point_calls:
        assert argv[:4] == ["git", "-c", "log.showSignature=false", "-C"]
        assert "--no-renames" in argv
        assert "--no-merges" not in argv
