"""Library ``CrossSessionWarning`` emission (epic concave-pale-sheldrake, story
3d8a / Library: warnings.warn CrossSessionWarning from single-ticket functions).

Full held-out oracle for story S5. Asserts observable behavior only — the presence,
count, category, and message of warnings emitted on the stdlib ``warnings`` channel, and
that the underlying library call still returns normally. Never asserts internal structure.

Setup mirrors ``tests/unit/test_cross_session.py``: a real temporary tracker, acting
session controlled via ``REBAR_SESSION_ID``; claim as holder A, then act as B.
"""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.unit

_SESSION_VARS = ("REBAR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID", "SESSION_ID")


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in _SESSION_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("AI_AGENT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    from rebar.config import reset_config_cache

    reset_config_cache()
    return repo


def _claim_as_a(repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create + claim a ticket as session A; return its id. Caller then acts as B."""
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-A")
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    rebar.claim(tid, assignee="alice", repo_root=str(repo))
    return tid


def _act_as_b(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")
    from rebar.config import reset_config_cache

    reset_config_cache()


def _cross_session_warnings(rec) -> list:
    return [w for w in rec if issubclass(w.category, rebar.CrossSessionWarning)]


# ---------------------------------------------------------------- happy path (implementer-visible)
def test_show_ticket_warns_for_other_session(rebar_repo, monkeypatch) -> None:
    """B calling show_ticket on an A-held ticket emits exactly one CrossSessionWarning
    naming holder A; the read still returns the ticket state."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    xs = _cross_session_warnings(rec)
    assert len(xs) == 1
    assert "sess-A" in str(xs[0].message)
    assert state is not None


def test_comment_warns_and_still_mutates(rebar_repo, monkeypatch) -> None:
    """B commenting on an A-held ticket emits a CrossSessionWarning AND the comment lands."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.comment(tid, "hello from B", repo_root=str(rebar_repo))
    xs = _cross_session_warnings(rec)
    assert len(xs) == 1
    assert "sess-A" in str(xs[0].message)
    # The mutation applied: the comment is present in the ticket's event stream.
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    dumped = str(state)
    assert "hello from B" in dumped


def test_same_session_is_silent(rebar_repo, monkeypatch) -> None:
    """Acting as the SAME session A that holds the ticket emits no CrossSessionWarning."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    # stay as session A
    from rebar.config import reset_config_cache

    reset_config_cache()
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.show_ticket(tid, repo_root=str(rebar_repo))
        rebar.comment(tid, "self note", repo_root=str(rebar_repo))
    assert _cross_session_warnings(rec) == []


# ---------------------------------------------------------------- held out
def test_transition_warns_for_other_session(rebar_repo, monkeypatch) -> None:
    """A single-ticket write (transition in_progress->closed) by B warns about holder A
    (computed pre-mutation), and the transition still applies."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    xs = _cross_session_warnings(rec)
    assert len(xs) == 1
    assert "sess-A" in str(xs[0].message)
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert "closed" in str(state)


def test_link_warns_for_primary_endpoint(rebar_repo, monkeypatch) -> None:
    """A two-endpoint op (link id1,id2) by B warns for the PRIMARY endpoint id1 (A-held),
    not id2; exactly one warning naming holder A."""
    id1 = _claim_as_a(rebar_repo, monkeypatch)
    # id2 created but unheld (session A context is fine; it is not claimed).
    id2 = rebar.create_ticket("task", "other", repo_root=str(rebar_repo))
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.link(id1, id2, "relates_to", repo_root=str(rebar_repo))
    xs = _cross_session_warnings(rec)
    assert len(xs) == 1
    assert "sess-A" in str(xs[0].message)


def test_bulk_reads_never_warn(rebar_repo, monkeypatch) -> None:
    """Bulk reads (list_tickets, ready) as B emit no CrossSessionWarning even with an
    A-held in_progress ticket present."""
    _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.list_tickets(repo_root=str(rebar_repo))
        rebar.ready(repo_root=str(rebar_repo))
    assert _cross_session_warnings(rec) == []


def test_unset_session_is_silent(rebar_repo, monkeypatch) -> None:
    """With REBAR_SESSION_ID unset, the acting session is unknown, so no warning fires."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    for var in _SESSION_VARS:
        monkeypatch.delenv(var, raising=False)
    from rebar.config import reset_config_cache

    reset_config_cache()
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert _cross_session_warnings(rec) == []


def test_warning_is_crosssessionwarning_subclass(rebar_repo, monkeypatch) -> None:
    """The emitted warning is an instance of rebar.CrossSessionWarning, itself a
    UserWarning subclass, so a caller can filter it distinctly."""
    assert issubclass(rebar.CrossSessionWarning, UserWarning)
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with pytest.warns(rebar.CrossSessionWarning):
        rebar.show_ticket(tid, repo_root=str(rebar_repo))


def test_reentrancy_exactly_one_warning(rebar_repo, monkeypatch) -> None:
    """The detector internally calls the public show_ticket; the re-entrancy guard must
    ensure exactly ONE CrossSessionWarning per top-level show_ticket call (not N, and no
    unbounded recursion)."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert len(_cross_session_warnings(rec)) == 1


def test_best_effort_detector_raises_still_returns(rebar_repo, monkeypatch) -> None:
    """When the detector raises, the library call still returns normally and emits no
    CrossSessionWarning — the advisory warning never breaks the operation."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("detector blew up")

    monkeypatch.setattr(
        "rebar._commands.cross_session.cross_session_warning_for", _boom, raising=True
    )
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state is not None
    assert _cross_session_warnings(rec) == []
