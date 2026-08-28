"""A SYMBOLIC ``--ref`` is pinned to one immutable sha for the whole verify+sign unit
(bug rookie-fickle-hydra, 3e21-4c5b-3e0d-4e23).

80af added the ``--ref`` carrier and made the pre-sign drift guard re-resolve THAT ref for its
fresh-sha read, reasoning that "a fixed-commit target is a stable no-op (its tree is immutable)".
True for a sha; false for a branch. 4de6 then pinned the DEFAULT (no ``--ref``) to one HEAD sha
at close entry but left an explicitly-supplied ref threaded through as its raw string, so
``--ref origin/main`` is rev-parsed TWICE — once for verification, once at the drift guard,
minutes apart. A concurrent ``git fetch`` in that window advances
``refs/remotes/origin/main``, the guard sees two differing SHAs, refuses to attest, and the
ticket closes WITHOUT a completion attestation while the command still exits 0.

``origin/main`` is strictly more exposed than the ``HEAD`` case 4de6 fixed: HEAD moves only when
someone commits in THIS worktree, while a remote-tracking ref lives in the shared common dir and
moves whenever anyone anywhere fetches.
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
    "to check.\n\n## Acceptance Criteria\n- [x] the story's change is present\n\n## Context\nc\n"
)

_TRACKING = "refs/remotes/origin/main"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


def _enable_completion_gate(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def _story_on_a_tracked_branch(repo: Path) -> tuple[str, str]:
    """A story whose own commit (A) carries its trailer, with ``origin/main`` tracking A."""
    story = rebar.create_ticket("task", "raced story", description=_DESC, repo_root=str(repo))
    (repo / "story.py").write_text("# story change\n")
    _git(repo, "add", "story.py")
    _git(repo, "commit", "-q", "-m", f"story change\n\nrebar-ticket: {story}")
    a = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", _TRACKING, a)
    rebar.transition(story, "open", "in_progress", repo_root=str(repo))
    return story, a


def test_symbolic_ref_close_pins_across_a_concurrent_fetch(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing with ``--ref origin/main`` while a concurrent fetch advances that remote-tracking
    ref mid-run must still land SIGNED, bound to the tree that was actually verified (A)."""
    _enable_completion_gate(rebar_repo)
    story, sha_a = _story_on_a_tracked_branch(rebar_repo)

    def racing_verify(ticket_id, *, ref=None, repo_root=None, **kwargs):
        # Resolve what the caller handed us FIRST — this is the tree the verifier checked...
        verified = resolve_ref(ref or "HEAD", repo_root, fetch=False)
        # ...then a concurrent `git fetch` elsewhere advances origin/main A -> B under us.
        root = Path(repo_root)
        (root / "unrelated.py").write_text("# someone else's landed work\n")
        _git(root, "add", "unrelated.py")
        _git(root, "commit", "-q", "-m", "unrelated work landed on main")
        _git(root, "update-ref", _TRACKING, _git(root, "rev-parse", "HEAD"))
        return {
            "verdict": "PASS",
            "findings": [],
            "runner": "fake",
            "model": "fake",
            "verified_at_sha": verified,
            "certifiable": True,
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", racing_verify)

    rebar.transition(story, "in_progress", "closed", ref="origin/main", repo_root=str(rebar_repo))

    sig = rebar.verify_signature(story, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig["verdict"] == "certified", sig
    # And it certified the tree the verifier actually read (A), never the advanced tip.
    assert sig["verified_at_sha"] == sha_a, sig


def test_symbolic_ref_close_reports_a_signed_completion_signature(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same race, read through the public marker: the caller must see ``signed`` rather than
    ``material_drifted`` — the payload field a consumer branches on."""
    _enable_completion_gate(rebar_repo)
    story, _sha_a = _story_on_a_tracked_branch(rebar_repo)

    def racing_verify(ticket_id, *, ref=None, repo_root=None, **kwargs):
        verified = resolve_ref(ref or "HEAD", repo_root, fetch=False)
        root = Path(repo_root)
        (root / "unrelated.py").write_text("# someone else's landed work\n")
        _git(root, "add", "unrelated.py")
        _git(root, "commit", "-q", "-m", "unrelated work landed on main")
        _git(root, "update-ref", _TRACKING, _git(root, "rev-parse", "HEAD"))
        return {
            "verdict": "PASS",
            "findings": [],
            "runner": "fake",
            "model": "fake",
            "verified_at_sha": verified,
            "certifiable": True,
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", racing_verify)

    out = rebar.transition(
        story, "in_progress", "closed", ref="origin/main", repo_root=str(rebar_repo)
    )

    assert out["completion_signature"] == {"signed": True, "cause": "signed", "error": ""}, out


def test_symbolic_ref_close_still_signs_when_the_branch_is_quiet(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: with no concurrent fetch, ``--ref origin/main`` signs against that branch's tip
    exactly as before — the pin must not change the target, only when it is resolved."""
    _enable_completion_gate(rebar_repo)
    story, sha_a = _story_on_a_tracked_branch(rebar_repo)

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

    rebar.transition(story, "in_progress", "closed", ref="origin/main", repo_root=str(rebar_repo))

    sig = rebar.verify_signature(story, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig["verdict"] == "certified", sig
    assert sig["verified_at_sha"] == sha_a, sig
