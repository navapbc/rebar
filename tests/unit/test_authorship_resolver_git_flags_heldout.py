"""Held-out signed-commit oracle for B5's authorship resolvers."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rebar.attest import authorship

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_signed_resolvers_return_only_the_introducing_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signature fixture is load-bearing: unfiltered git log emits signature text."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _git(tracker, "init", "--quiet", "--initial-branch=tickets")
    _git(tracker, "config", "user.email", "ci@example.com")
    _git(tracker, "config", "user.name", "CI fixture")
    private_key = tmp_path / "signing-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
        check=True,
        capture_output=True,
    )
    public_key = private_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    allowed = tmp_path / "allowed-signers"
    allowed.write_text(f"ci@example.com {public_key}\n", encoding="utf-8")
    _git(tracker, "config", "gpg.format", "ssh")
    _git(tracker, "config", "user.signingkey", str(private_key))
    _git(tracker, "config", "gpg.ssh.allowedSignersFile", str(allowed))
    _git(tracker, "config", "commit.gpgsign", "true")
    _git(tracker, "config", "log.showSignature", "true")

    ticket_dir = tracker / "ticket"
    ticket_dir.mkdir()
    position = "1700000000000000000-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (ticket_dir / f"{position}-CREATE.json").write_text("{}\n", encoding="utf-8")
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "--quiet", "-m", "signed event")
    expected = _git(tracker, "rev-parse", "HEAD")

    unfiltered = _git(
        tracker,
        "log",
        "--diff-filter=A",
        "--full-history",
        "--format=%H",
        "--",
        f"ticket/{position}-*.json",
    )
    assert len(unfiltered.splitlines()) > 1, "signed fixture must expose signature display lines"

    from rebar._commands import _seam

    monkeypatch.setattr(_seam, "tracker_dir", lambda repo_root=None: tracker)
    real_run = subprocess.run
    resolver_calls: list[list[str]] = []

    def recording_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        resolver_calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(authorship, "subprocess", SimpleNamespace(run=recording_run))
    resolved_event = authorship.resolve_event_commit(position, str(ticket_dir))
    resolved_position = authorship.resolve_position_commit(position, str(tracker))
    assert resolved_event == expected
    assert resolved_position == expected
    assert re.fullmatch(r"[0-9a-f]{40}", resolved_event or "")
    assert re.fullmatch(r"[0-9a-f]{40}", resolved_position or "")
    assert len(resolver_calls) == 2
    for argv in resolver_calls:
        assert argv[:4] == ["git", "-c", "log.showSignature=false", "-C"]
        assert "--no-renames" in argv
