"""Close-time completion verification can target a specific commit via `--ref` (bug 80af).

The completion-verification close gate verifies the committed tree at the worktree HEAD
(`_completion_precheck` calls `verify_completion(..., ref="HEAD")`). When landing a STACKED
epic, closing each story against the cumulative tip mis-scopes per-story acceptance criteria
(later stories' changes are present at the tip). The target IS reachable by checking out the
story's own commit, but that lever was not surfaced from the close workflow.

This wires a `--ref` through `rebar transition ... closed` (default HEAD = unchanged) so a
stacked story can be verified — AND SIGNED — against its own commit while the worktree stays
at the tip. The signing half matters: the post-close pre-sign drift check must resolve the
SAME ref (not HEAD), otherwise `verified-at-sha` (the story commit) != HEAD (the tip) and the
close would land WITHOUT a completion signature — which the child-closure trust gate needs.

Contract pinned here (observable transition / verify_completion behavior):

* `transition(..., "closed", ref=<story-sha>)` forwards that ref to the completion verifier.
* closing a story at its own commit while HEAD is at the tip lands SIGNED (no drift), so no
  manual operator-attestation is needed.
* absent `--ref`, the target is HEAD exactly as before (no behavior change).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar._commands import transition as _transition
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


def _stub_verifier(monkeypatch: pytest.MonkeyPatch, calls: list[dict]) -> None:
    """Replace the close-gate's LLM verifier with an offline PASS that faithfully resolves the
    ref it is handed (mirroring real attested verify_completion) so `verified_at_sha` reflects
    the targeted commit — and record every call's ref for forwarding assertions."""

    def fake_verify_completion(ticket_id, *, ref=None, repo_root=None, **kwargs):
        calls.append({"ticket_id": ticket_id, "ref": ref})
        sha = resolve_ref(ref or "HEAD", repo_root, fetch=False)
        return {
            "verdict": "PASS",
            "findings": [],
            "runner": "fake",
            "model": "fake",
            "verified_at_sha": sha,
            "certifiable": True,
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", fake_verify_completion)


def _make_stack(repo: Path) -> tuple[str, str, str]:
    """A leaf story whose own commit (sha1) records its change; a later commit (sha2) sits on
    top so HEAD is the cumulative tip. Returns (story_id, sha1, sha2)."""
    story = rebar.create_ticket("task", "stacked story", description=_DESC, repo_root=str(repo))
    (repo / "story.py").write_text("# story change\n")
    _git(repo, "add", "story.py")
    _git(repo, "commit", "-q", "-m", f"story change\n\nrebar-ticket: {story}")
    sha1 = _git(repo, "rev-parse", "HEAD")
    (repo / "later.py").write_text("# a later story's change\n")
    _git(repo, "add", "later.py")
    _git(repo, "commit", "-q", "-m", "later story change")
    sha2 = _git(repo, "rev-parse", "HEAD")
    rebar.transition(story, "open", "in_progress", repo_root=str(repo))
    return story, sha1, sha2


def test_close_ref_forwards_to_verifier_and_signs_at_target(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Closing the story with ref=<its own commit> while HEAD is at the tip: the verifier is
    invoked with that ref, and the close lands SIGNED against it (no drift, no manual step)."""
    _enable_completion_gate(rebar_repo)
    story, sha1, sha2 = _make_stack(rebar_repo)
    assert sha1 != sha2

    calls: list[dict] = []
    _stub_verifier(monkeypatch, calls)
    rebar.transition(story, "in_progress", "closed", ref=sha1, repo_root=str(rebar_repo))

    assert calls and calls[-1]["ref"] == sha1, calls
    err = capsys.readouterr().err.lower()
    assert "drifted" not in err, err
    assert (
        rebar.verify_signature(story, kind="completion-verifier", repo_root=str(rebar_repo))[
            "verdict"
        ]
        == "certified"
    )


def test_close_without_ref_targets_head(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent --ref, the completion target is HEAD — now resolved to HEAD's immutable sha ONCE
    at close entry (bug 4de6) rather than the lazy string "HEAD", so the observable target is
    the current HEAD commit (sha2), not a re-resolvable symbol."""
    _enable_completion_gate(rebar_repo)
    story, _sha1, sha2 = _make_stack(rebar_repo)

    calls: list[dict] = []
    _stub_verifier(monkeypatch, calls)
    rebar.transition(story, "in_progress", "closed", repo_root=str(rebar_repo))

    assert calls and calls[-1]["ref"] == sha2, calls
    assert (
        rebar.verify_signature(story, kind="completion-verifier", repo_root=str(rebar_repo))[
            "verdict"
        ]
        == "certified"
    )


def test_parse_flags_reads_ref() -> None:
    """The CLI flag parser accepts --ref <v> and --ref=<v>, and yields nothing when absent."""
    parsed_space = _transition._parse_flags(["--ref", "abc123"])
    parsed_eq = _transition._parse_flags(["--ref=abc123"])
    parsed_absent = _transition._parse_flags(["--class=regression"])
    assert "abc123" in parsed_space, parsed_space
    assert "abc123" in parsed_eq, parsed_eq
    assert "abc123" not in parsed_absent, parsed_absent
