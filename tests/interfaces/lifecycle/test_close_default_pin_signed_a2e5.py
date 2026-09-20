"""Default completion close pins HEAD at entry and signs that pinned SHA (bug a2e5)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar._snapshot.repo_snapshot import resolve_ref

_DESC = (
    "Body with enough detail to be a real plan describing the change so the gate has something "
    "to check.\n\n## Acceptance Criteria\n- [x] the story's change is present\n\n## Context\nc\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


def _enable_completion_gate(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def _in_progress_story(repo: Path) -> tuple[str, str]:
    story = rebar.create_ticket("task", "default-pin story", description=_DESC, repo_root=str(repo))
    (repo / "story.py").write_text("# story change\n")
    _git(repo, "add", "story.py")
    _git(repo, "commit", "-q", "-m", f"story change\n\nrebar-ticket: {story}")
    pinned_sha = _git(repo, "rev-parse", "HEAD")
    rebar.transition(story, "open", "in_progress", repo_root=str(repo))
    return story, pinned_sha


def test_default_close_signs_the_entry_pin_when_head_moves(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --ref still certifies the close against the entry-time HEAD, even after HEAD moves."""
    _enable_completion_gate(rebar_repo)
    story, pinned_sha = _in_progress_story(rebar_repo)

    def racing_verify(ticket_id, *, ref=None, repo_root=None, **kwargs):
        verified = resolve_ref(ref or "HEAD", repo_root, fetch=False)
        root = Path(repo_root)
        (root / "unrelated.py").write_text("# unrelated concurrent work\n")
        _git(root, "add", "unrelated.py")
        _git(root, "commit", "-q", "-m", "unrelated concurrent work")
        return {
            "verdict": "PASS",
            "findings": [],
            "runner": "fake",
            "model": "fake",
            "verified_at_sha": verified,
            "certifiable": True,
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", racing_verify)

    out = rebar.transition(story, "in_progress", "closed", repo_root=str(rebar_repo))

    current_head = _git(rebar_repo, "rev-parse", "HEAD")
    assert current_head != pinned_sha
    assert out["completion_signature"] == {"signed": True, "cause": "signed", "error": ""}, out
    sig = rebar.verify_signature(story, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig["verdict"] == "certified", sig
    assert sig["verified_at_sha"] == pinned_sha, sig
