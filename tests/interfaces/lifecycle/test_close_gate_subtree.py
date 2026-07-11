"""Subtree-aware referencing-commit precondition (bug ferric-jet-scorpion / 1edf).

The completion-gate's deterministic referencing-commit precondition
(``rebar._commands.transition_close._completion_precheck`` /
``_referencing_commit_exists``) must credit a parent's ENTIRE descendant subtree: a
ticket that records ``file_impact`` closes when a ``rebar-ticket:`` trailer references it
OR any of its descendants. A parent's code is delivered by its children's commits, so an
epic/story must not be forced into ``--force-close`` (unsigned) merely because the
referencing commits carry the child ids.

This is safe: the open-children guard runs first, so a parent only reaches this
precondition once every child is closed — and each child already passed this exact
referencing-commit check at its own close.

Happy path here; the transitive-grandchild, still-blocks-without-subtree-commit, and
leaf no-regression cases are validated by the held-out companion suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import rebar
import rebar.llm

_DESC = (
    "Body.\n\n## Acceptance Criteria\n- [ ] done\n\n## Success Criteria\n- [ ] x\n\n## Context\nc\n"
)


def _enable(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def PASS(ticket_id, **kw):  # noqa: N802 — mirrors test_completion_gate.py
    return {"verdict": "PASS", "findings": [], "runner": "fake", "model": "m"}


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _commit_ref(repo: Path, ref: str) -> None:
    """Empty commit whose message carries a ``rebar-ticket: <ref>`` trailer."""
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


def test_parent_closes_when_a_child_commit_references_the_subtree(
    rebar_repo: Path, monkeypatch
) -> None:
    """An epic that records file_impact but whose only referencing commit carries the CHILD's
    id closes: the precondition credits the descendant subtree, the verifier runs, and the
    epic closes signed — no --force-close needed."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    child = _mk(rebar_repo, "task", parent=epic)  # no file_impact -> child closes freely

    # Move the leaf into progress (cascades the open parent into progress too), then commit
    # a trailer referencing ONLY the child, and close the child.
    rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, child)
    _close(child, rebar_repo)
    assert _status(child, rebar_repo) == "closed"

    # The epic records file_impact and NO commit references the epic's own id — only the
    # child's. With the subtree fix the precondition passes and the epic closes signed.
    _close(epic, rebar_repo)
    assert _status(epic, rebar_repo) == "closed"
    assert rebar.verify_signature(epic, repo_root=str(rebar_repo))["verdict"] == "certified"
