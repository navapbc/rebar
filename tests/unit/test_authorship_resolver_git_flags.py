"""Happy-path command contract for the per-event authorship resolvers (B5)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.attest import authorship

pytestmark = pytest.mark.unit


def test_both_resolvers_match_bulk_git_safety_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / "tracker"
    ticket_dir = tracker / "ticket"
    ticket_dir.mkdir(parents=True)
    sha = "a" * 40
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{sha}\n", "")

    from rebar._commands import _seam

    monkeypatch.setattr(_seam, "tracker_dir", lambda repo_root=None: tracker)
    monkeypatch.setattr(authorship, "subprocess", SimpleNamespace(run=fake_run))

    assert authorship.resolve_event_commit("123-abc", str(ticket_dir)) == sha
    assert authorship.resolve_position_commit("123-abc", str(tracker)) == sha
    assert len(calls) == 2
    for argv in calls:
        assert argv[:4] == ["git", "-c", "log.showSignature=false", "-C"]
        assert "--no-renames" in argv
        assert "--no-merges" not in argv
