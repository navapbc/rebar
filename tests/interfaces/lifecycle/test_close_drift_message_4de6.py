"""The residual unsigned-close drift warning must name a WORKING recovery (bug 4de6, AC #3).

When a completion close DOES land unsigned (now only reachable in the fallback path — if the
entry-time ``resolve_ref("HEAD")`` throws, the default drops to the lazy head-read and a
concurrent commit can still split verify from sign), the warning used to say "Re-close to
certify against the current tree." That is a no-op: the ticket is already ``closed``, so
``transition <id> in_progress closed`` errors and ``transition <id> closed closed`` is a no-op.
The message must instead point at a recovery that actually works: ``rebar reopen`` then a
re-close.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm

_DESC = (
    "Body with enough detail to be a real plan describing the change so the gate has something "
    "to check.\n\n## Acceptance Criteria\n- [ ] the story's change is present\n\n## Context\nc\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


def _enable_completion_gate(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def _in_progress_story(repo: Path) -> str:
    story = rebar.create_ticket("task", "drift-msg story", description=_DESC, repo_root=str(repo))
    (repo / "story.py").write_text("# story change\n")
    _git(repo, "add", "story.py")
    _git(repo, "commit", "-q", "-m", f"story change\n\nrebar-ticket: {story}")
    rebar.transition(story, "open", "in_progress", repo_root=str(repo))
    return story


def test_drift_warning_names_a_working_recovery(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Force the fallback drift path (entry-pin resolve_ref throws → lazy HEAD re-read → a
    concurrent commit splits verify/sign). The close lands unsigned, and the warning must name
    a WORKING recovery (`rebar reopen` → re-close), NOT the no-op "Re-close ... current tree"."""
    _enable_completion_gate(rebar_repo)
    story = _in_progress_story(rebar_repo)

    # Break the entry-time HEAD pin so ref stays None and the drift guard falls to head_sha().
    import rebar._snapshot.repo_snapshot as rs

    def _boom(*a, **k):
        raise RuntimeError("resolve_ref unavailable")

    monkeypatch.setattr(rs, "resolve_ref", _boom)

    def racing_verify(ticket_id, *, ref=None, repo_root=None, **kwargs):
        root = Path(repo_root)
        verified = _git(root, "rev-parse", "HEAD")  # A, what the verifier saw
        (root / "unrelated.py").write_text("# in-flight work\n")
        _git(root, "add", "unrelated.py")
        _git(root, "commit", "-q", "-m", "unrelated in-flight work")  # HEAD A -> B
        return {
            "verdict": "PASS",
            "findings": [],
            "runner": "fake",
            "model": "fake",
            "verified_at_sha": verified,
            "certifiable": True,
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", racing_verify)

    rebar.transition(story, "in_progress", "closed", repo_root=str(rebar_repo))

    # It genuinely landed unsigned (the fallback race), so the warning path fired.
    sig = rebar.verify_signature(story, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig["verdict"] != "certified", sig
    msg = capsys.readouterr().err.lower()
    assert "reopen" in msg, msg  # a recovery that actually works on a closed ticket
    assert "re-close to certify against the current tree" not in msg, msg  # the old no-op
