"""The DEFAULT completion close pins HEAD to one immutable sha for the whole verify+sign unit
(bug 4de6). Without ``--ref``, the close used to resolve ``HEAD`` LAZILY twice — once when the
verifier ran, once at the pre-sign drift guard — so a benign concurrent commit in that window
split them: verify checked commit A (the ticket's real work), sign read HEAD→B (an unrelated
agent's in-flight commit in the shared worktree), the drift guard refused to attest a tree it
had not verified, and the ticket closed WITHOUT a completion attestation with no non-``--force``
recovery.

80af added the ``--ref`` carrier (verify + drift guard both bind a passed ref). This makes the
DEFAULT (no ``--ref``) reuse that carrier: resolve ``HEAD`` to a sha ONCE at close entry and
thread it, so verify and sign bind the SAME immutable commit and a concurrent commit can no
longer split them. The default still targets HEAD — it is just resolved once, not twice.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar._snapshot.repo_snapshot import resolve_ref

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


def _in_progress_story(repo: Path) -> tuple[str, str]:
    """A story whose own commit (A = HEAD) carries its rebar-ticket trailer; returns (id, A)."""
    story = rebar.create_ticket("task", "raced story", description=_DESC, repo_root=str(repo))
    (repo / "story.py").write_text("# story change\n")
    _git(repo, "add", "story.py")
    _git(repo, "commit", "-q", "-m", f"story change\n\nrebar-ticket: {story}")
    a = _git(repo, "rev-parse", "HEAD")
    rebar.transition(story, "open", "in_progress", repo_root=str(repo))
    return story, a


def test_default_close_pins_head_across_a_concurrent_commit(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent, UNRELATED commit landing during the verify step must NOT split verify from
    sign: the DEFAULT close (no --ref) still lands SIGNED, against the ticket's real tree (A)."""
    _enable_completion_gate(rebar_repo)
    story, sha_a = _in_progress_story(rebar_repo)

    def racing_verify(ticket_id, *, ref=None, repo_root=None, **kwargs):
        # Resolve what the caller handed us FIRST (this is the tree the verifier checked)...
        verified = resolve_ref(ref or "HEAD", repo_root, fetch=False)
        # ...then an unrelated agent commits into the shared worktree, moving HEAD A -> B.
        root = Path(repo_root)
        (root / "unrelated.py").write_text("# another ticket's in-flight work\n")
        _git(root, "add", "unrelated.py")
        _git(root, "commit", "-q", "-m", "unrelated in-flight work")
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

    sig = rebar.verify_signature(story, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig["verdict"] == "certified", sig
    # And it certified the ticket's REAL tree (A), not the unrelated commit (B).
    assert sig["verified_at_sha"] == sha_a, sig


def test_default_close_still_signs_when_head_is_quiet(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: with no concurrent commit, the default close signs against HEAD exactly as before."""
    _enable_completion_gate(rebar_repo)
    story, sha_a = _in_progress_story(rebar_repo)

    def quiet_verify(ticket_id, *, ref=None, repo_root=None, **kwargs):
        return {
            "verdict": "PASS",
            "findings": [],
            "runner": "fake",
            "model": "fake",
            "verified_at_sha": resolve_ref(ref or "HEAD", repo_root, fetch=False),
            "certifiable": True,
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", quiet_verify)

    rebar.transition(story, "in_progress", "closed", repo_root=str(rebar_repo))

    sig = rebar.verify_signature(story, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig["verdict"] == "certified", sig
    assert sig["verified_at_sha"] == sha_a, sig
