"""add5 (epic 7738): register the `session_log` ticket type + its write-path rules.

A `session_log` can be created and edited/commented, but it is gate-exempt, cannot
be claimed or transitioned (lifecycle-exempt), and rejects blocking links
(blocks/depends_on) on either endpoint while allowing the non-blocking relations.
These are exercised end-to-end through the library API (which CLI + MCP funnel
through), so one assertion covers all three interfaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import RebarError

pytestmark = pytest.mark.interface


def _mk_log(repo: Path, *, title: str = "Session log: did stuff", description: str = "") -> str:
    return rebar.create_ticket("session_log", title, description=description, repo_root=str(repo))


# ── type registration ─────────────────────────────────────────────────────────
def test_create_session_log_succeeds(rebar_repo: Path) -> None:
    tid = _mk_log(rebar_repo)
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["ticket_type"] == "session_log"


def test_invalid_type_still_rejected(rebar_repo: Path) -> None:
    """The enum widening must not accept arbitrary types."""
    with pytest.raises(RebarError):
        rebar.create_ticket("logbook", "nope", repo_root=str(rebar_repo))


# ── lifecycle exemption: no claim, no transition ──────────────────────────────
def test_claim_refused(rebar_repo: Path) -> None:
    tid = _mk_log(rebar_repo)
    with pytest.raises(RebarError, match="session_log tickets are lifecycle-exempt"):
        rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))


def test_transition_refused(rebar_repo: Path) -> None:
    tid = _mk_log(rebar_repo)
    with pytest.raises(RebarError, match="session_log tickets are lifecycle-exempt"):
        rebar.transition(tid, "open", "closed", repo_root=str(rebar_repo))


def test_transition_noop_also_refused(rebar_repo: Path) -> None:
    """A same-status transition short-circuits to a no-op before the core guard, so
    it must be refused at the compute layer too (else it reports a spurious success)."""
    tid = _mk_log(rebar_repo)
    with pytest.raises(RebarError, match="session_log tickets are lifecycle-exempt"):
        rebar.transition(tid, "open", "open", repo_root=str(rebar_repo))


# ── link rules: blocking refused (both endpoints), non-blocking allowed ────────
def test_blocking_links_refused_both_directions(rebar_repo: Path) -> None:
    log = _mk_log(rebar_repo)
    task = rebar.create_ticket("task", "Real work", description="body", repo_root=str(rebar_repo))
    with pytest.raises(RebarError, match="session_log"):
        rebar.link(log, task, "blocks", repo_root=str(rebar_repo))
    with pytest.raises(RebarError, match="session_log"):
        rebar.link(task, log, "depends_on", repo_root=str(rebar_repo))


@pytest.mark.parametrize("relation", ["relates_to", "discovered_from"])
def test_nonblocking_links_allowed(rebar_repo: Path, relation: str) -> None:
    log = _mk_log(rebar_repo)
    task = rebar.create_ticket("task", "Real work", description="body", repo_root=str(rebar_repo))
    rebar.link(log, task, relation, repo_root=str(rebar_repo))  # must not raise
    deps = rebar.deps(log, repo_root=str(rebar_repo))
    assert any(d["relation"] == relation for d in deps.get("deps", []))


# ── gate exemption (pass regardless of body shape) ────────────────────────────
def test_gates_exempt_on_empty_body(rebar_repo: Path) -> None:
    # No '## Acceptance Criteria', tiny body — would FAIL the gates for a task.
    log = _mk_log(rebar_repo, description="just a log line")
    assert rebar.clarity_check(log, repo_root=str(rebar_repo))["verdict"] == "pass"
    ac = rebar.check_ac(log, repo_root=str(rebar_repo))
    assert ac["verdict"] == "pass" and "exempt" in ac["reason"]
    qc = rebar.quality_check(log, repo_root=str(rebar_repo))
    assert qc["verdict"] == "pass" and "exempt" in qc["reason"]


# ── show / comment / edit operate normally ────────────────────────────────────
def test_show_comment_edit_work(rebar_repo: Path) -> None:
    log = _mk_log(rebar_repo, description="initial")
    rebar.comment(log, "appended a verbose entry", repo_root=str(rebar_repo))
    rebar.edit_ticket(log, description="updated body", repo_root=str(rebar_repo))
    state = rebar.show_ticket(log, repo_root=str(rebar_repo))
    assert state["description"] == "updated body"
    assert any("appended a verbose entry" in c.get("body", "") for c in state.get("comments", []))
