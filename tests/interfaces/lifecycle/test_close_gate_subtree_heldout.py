"""Held-out oracle for the subtree-aware referencing-commit precondition (1edf).

The implementer does NOT see this file. It pins:
  * transitivity — a grandchild (task) commit credits the epic (epic->story->task);
  * the guard still BITES — an epic with file_impact and NO referencing commit anywhere in
    its subtree still fails closed (the fix credits descendants, it does not disable);
  * leaf no-regression — a leaf (no descendants) with file_impact still blocks without its
    own commit and still closes with it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm

_DESC = (
    "Body.\n\n## Acceptance Criteria\n- [ ] done\n\n## Success Criteria\n- [ ] x\n\n## Context\nc\n"
)


def _enable(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def PASS(ticket_id, **kw):  # noqa: N802
    return {"verdict": "PASS", "findings": [], "runner": "fake", "model": "m"}


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "unrelated"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _commit_ref(repo: Path, ref: str) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", f"work\n\nrebar-ticket: {ref}"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _mk(repo: Path, ttype: str, *, parent: str | None = None, file_impact: bool = False) -> str:
    tid = rebar.create_ticket(
        ttype, f"{ttype} t", description=_DESC, parent=parent, repo_root=str(repo)
    )
    if file_impact:
        rebar.set_file_impact(tid, [{"path": "src/x.py", "reason": "touched"}], repo_root=str(repo))
    return tid


def _close(tid: str, repo: Path) -> None:
    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))


def test_grandchild_commit_credits_epic_transitively(rebar_repo: Path, monkeypatch) -> None:
    """epic -> story -> task; the only referencing commit carries the TASK (grandchild) id.
    The epic's precondition must credit the transitive subtree and close signed."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    story = _mk(rebar_repo, "story", parent=epic)
    task = _mk(rebar_repo, "task", parent=story)

    # Progress the leaf (cascades story + epic into progress), commit against the grandchild
    # only, then close bottom-up.
    rebar.transition(task, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, task)
    _close(task, rebar_repo)
    _close(story, rebar_repo)

    _close(epic, rebar_repo)
    assert _status(epic, rebar_repo) == "closed"
    assert rebar.verify_signature(epic, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_epic_with_file_impact_and_no_subtree_commit_still_blocks(
    rebar_repo: Path, monkeypatch
) -> None:
    """The fix credits descendants; it does NOT disable the guard. An epic with file_impact
    and NO commit referencing it OR any descendant is still blocked. The block is the
    DETERMINISTIC precondition (its message names file_impact + a commit), asserted distinct
    from any verifier outcome — so a PASS verifier (which the no-file_impact child close
    legitimately calls) can never mask a missing epic-subtree block."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    child = _mk(rebar_repo, "task", parent=epic)  # no file_impact, no referencing commit

    rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))
    _commit(rebar_repo)  # a HEAD commit exists but references nothing in the subtree
    _close(child, rebar_repo)

    with pytest.raises(rebar.RebarError) as ei:
        _close(epic, rebar_repo)
    assert ei.value.returncode == 1
    # The DETERMINISTIC precondition message (not a verifier-failure message).
    assert "file_impact" in ei.value.stderr
    assert "commit" in ei.value.stderr.lower()
    assert _status(epic, rebar_repo) == "in_progress"
    assert rebar.verify_signature(epic, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_leaf_with_file_impact_and_no_commit_still_blocks(rebar_repo: Path, monkeypatch) -> None:
    """No-regression: a leaf (no descendants) with file_impact and no referencing commit
    still fails closed under the subtree code path. Uses a PASS verifier + a message assert
    so a disabled guard (which would let the close reach the PASS verifier and succeed) is
    caught, rather than being masked by a verifier-error block."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    leaf = _mk(rebar_repo, "task", file_impact=True)
    rebar.transition(leaf, "open", "in_progress", repo_root=str(rebar_repo))
    _commit(rebar_repo)  # non-referencing

    with pytest.raises(rebar.RebarError) as ei:
        _close(leaf, rebar_repo)
    assert ei.value.returncode == 1
    assert "file_impact" in ei.value.stderr  # the DETERMINISTIC block, not a verifier block
    assert _status(leaf, rebar_repo) == "in_progress"


def test_leaf_with_file_impact_and_own_commit_closes(rebar_repo: Path, monkeypatch) -> None:
    """No-regression: a leaf with file_impact and its OWN referencing commit still closes."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    leaf = _mk(rebar_repo, "task", file_impact=True)
    rebar.transition(leaf, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, leaf)
    _close(leaf, rebar_repo)
    assert _status(leaf, rebar_repo) == "closed"
    assert rebar.verify_signature(leaf, repo_root=str(rebar_repo))["verdict"] == "certified"
