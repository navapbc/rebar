"""`idea → closed` bypasses the completion/attestation close gates (story eced).

Closing a ticket that is in `idea` is a REJECT/DROP ("we won't pursue this
undesigned idea"), not a completion — so there is nothing to verify or attest, and
the close must succeed with NONE of the completion gates that guard a real close:

- the bug-close-reason guard (``txn.transition_core``),
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


def _enable_completion_gate(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


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
def test_idea_story_epic_closes(rebar_repo: Path, ttype: str) -> None:
    """An `idea` story/epic is a reject/drop and closes cleanly (nothing to verify)."""
    tid = rebar.create_ticket(ttype, f"Rough {ttype} idea", repo_root=str(rebar_repo))
    rebar.transition(tid, "open", "idea", repo_root=str(rebar_repo))

    out = rebar.transition(tid, "idea", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"


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


# ── the completion-verifier bypass, with the gate ENABLED (spy assertions) ────────
def test_idea_close_does_not_invoke_completion_verifier(rebar_repo: Path, monkeypatch) -> None:
    """With the completion-verification gate ON, an `idea → closed` transition must NOT
    invoke the verifier (the transition_close bypass), while a non-idea close under the
    SAME gate DOES reach it — the discriminator that proves the bypass is real (not the
    gate being off). The gate calls `rebar.llm.verify_completion` by module attribute."""
    import rebar.llm

    _enable_completion_gate(rebar_repo)
    _commit(rebar_repo)

    def _never(ticket_id, **kw):  # must NOT be called for an idea close
        raise AssertionError("completion verifier was invoked for an idea->closed transition")

    monkeypatch.setattr(rebar.llm, "verify_completion", _never)

    idea = rebar.create_ticket("task", "Parked idea", repo_root=str(rebar_repo))
    rebar.transition(idea, "open", "idea", repo_root=str(rebar_repo))
    out = rebar.transition(idea, "idea", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"  # bypassed: verifier never called, close succeeds

    # Discriminator: a NON-idea close under the same gate reaches the verifier, so the
    # _never spy fires and the close does NOT succeed — proving the idea arm truly bypassed.
    work = rebar.create_ticket("task", "Real work", repo_root=str(rebar_repo))
    rebar.transition(work, "open", "in_progress", repo_root=str(rebar_repo))
    with pytest.raises((AssertionError, rebar.RebarError)):
        rebar.transition(work, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(work, rebar_repo) != "closed"


def test_idea_close_skips_completion_precheck_including_file_impact(
    rebar_repo: Path, monkeypatch
) -> None:
    """The file-impact→referencing-commit precheck lives INSIDE `_completion_precheck`
    (transition_close), which an `idea → closed` transition skips wholesale. Patching the
    precheck to fire proves neither the verifier nor the file-impact check runs for idea —
    even for an idea ticket that has file_impact recorded but no referencing commit (which
    the precheck would otherwise block)."""
    from rebar._commands import transition_close

    _enable_completion_gate(rebar_repo)
    _commit(rebar_repo)

    def _never_precheck(*a, **k):
        raise AssertionError("_completion_precheck (verifier + file-impact) ran for idea close")

    monkeypatch.setattr(transition_close, "_completion_precheck", _never_precheck)

    idea = rebar.create_ticket("task", "Parked idea", repo_root=str(rebar_repo))
    rebar.set_file_impact(
        idea, [{"path": "src/x.py", "reason": "touch"}], repo_root=str(rebar_repo)
    )
    rebar.transition(idea, "open", "idea", repo_root=str(rebar_repo))
    out = rebar.transition(idea, "idea", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"  # whole precheck skipped for idea
