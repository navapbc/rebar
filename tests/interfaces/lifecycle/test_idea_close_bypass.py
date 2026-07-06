"""`idea → closed` bypasses the completion/attestation close gates (story eced).

Closing a ticket that is in `idea` is a REJECT/DROP ("we won't pursue this
undesigned idea"), not a completion — so there is nothing to verify or attest, and
the close must succeed with NONE of the completion gates that guard a real close:

- the bug-close-reason guard (``txn.transition_core``),
- the opt-in story/epic signature gate (``txn.transition_core``),
- the completion-verifier / file-impact precheck (``transition_close.close_ticket``).

The one guard that STAYS is the structural open-children guard (integrity, not
completion): an `idea` parent over non-closed children is still refused. Non-`idea`
close paths are unchanged (regression-pinned here).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar


def _enable_signature_gate(repo: Path) -> None:
    (repo / ".rebar").mkdir(exist_ok=True)
    (repo / ".rebar" / "config.conf").write_text("verify.require_signature_for_close=true\n")


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def test_idea_bug_closes_without_reason(rebar_repo: Path) -> None:
    """A bug in `idea` closes with NO --reason (bug-close-reason guard bypassed)."""
    tid = rebar.create_ticket("bug", "Rough bug idea", repo_root=str(rebar_repo))
    rebar.transition(tid, "open", "idea", repo_root=str(rebar_repo))

    out = rebar.transition(tid, "idea", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"


def test_non_idea_bug_still_requires_reason(rebar_repo: Path) -> None:
    """Regression: a normal bug close still requires a --reason (guard intact)."""
    tid = rebar.create_ticket("bug", "Real bug", repo_root=str(rebar_repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))

    with pytest.raises(rebar.RebarError, match="reason"):
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) != "closed"


@pytest.mark.parametrize("ttype", ("story", "epic"))
def test_idea_story_epic_closes_without_signature(rebar_repo: Path, ttype: str) -> None:
    """With the signature gate ON, an `idea` story/epic still closes unsigned."""
    _enable_signature_gate(rebar_repo)
    _commit(rebar_repo)
    tid = rebar.create_ticket(ttype, f"Rough {ttype} idea", repo_root=str(rebar_repo))
    rebar.transition(tid, "open", "idea", repo_root=str(rebar_repo))

    out = rebar.transition(tid, "idea", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"


def test_non_idea_story_still_gated_by_signature(rebar_repo: Path) -> None:
    """Regression: with the gate ON, a normal story close without a signature fails."""
    _enable_signature_gate(rebar_repo)
    _commit(rebar_repo)
    tid = rebar.create_ticket("story", "Real story", repo_root=str(rebar_repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))

    with pytest.raises(rebar.RebarError):
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) != "closed"


def test_idea_parent_open_children_guard_still_holds(rebar_repo: Path) -> None:
    """The structural open-children guard is NOT relaxed for `idea → closed`."""
    parent = rebar.create_ticket("epic", "Idea epic", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "Open child", parent=parent, repo_root=str(rebar_repo))
    rebar.transition(parent, "open", "idea", repo_root=str(rebar_repo))

    with pytest.raises(rebar.RebarError):
        rebar.transition(parent, "idea", "closed", repo_root=str(rebar_repo))
    assert _status(parent, rebar_repo) != "closed"

    # Once the child is closed, the idea parent can be rejected/closed.
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))
    out = rebar.transition(parent, "idea", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"
