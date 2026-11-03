"""A close's explicit ``--caused-by`` obeys ``link``'s referencing-commit rule.

Bug primitive-flowered-panther.

``rebar link <bug> <target> caused_by`` refuses a target that no commit references, because
such a target cannot be shown to have introduced anything. A bug close with ``--caused-by``
wrote the same edge through the low-level writer and skipped that rule, recording an
``explicit`` attribution ``link`` would refuse. The close now refuses before writing anything,
the way it already refuses an unresolvable value (surprising-fruticose-herring).

The rule applies only where the close would write an explicit edge: a blame-derived culprit is
named by a commit's own trailer, a non-bug close records no edge, and an already-active target
is left alone. History that cannot prove absence allows the edge, exactly as on ``link``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import RebarError

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit(repo: Path, message: str) -> None:
    _git(repo, "commit", "--allow-empty", "-q", "-m", message)


@pytest.fixture
def empty_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialized store in a git repo with no remote and no code commits yet."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


@pytest.fixture
def rebar_repo(empty_repo: Path) -> Path:
    """The same store after 1 unrelated commit: with no remote, absence is proven."""
    _commit(empty_repo, "unrelated base commit")
    return empty_repo


def _caused_by(tid: str, repo: str) -> list[dict]:
    deps = rebar.show_ticket(tid, repo_root=repo)["deps"]
    return [d for d in deps if d["relation"] == "caused_by"]


def _in_progress(ticket_type: str, repo: str) -> str:
    tid = rebar.create_ticket(ticket_type, f"the {ticket_type}", repo_root=repo)
    rebar.transition(tid, "open", "in_progress", repo_root=repo)
    return tid


def _close(tid: str, repo: str, caused_by: str = "", close_class: str = "regression") -> dict:
    return rebar.transition(
        tid,
        "in_progress",
        "closed",
        close_class=close_class,
        caused_by=caused_by,
        repo_root=repo,
    )


def test_commitless_explicit_target_refuses_the_close(rebar_repo: Path) -> None:
    repo = str(rebar_repo)
    target = rebar.create_ticket("task", "never shipped a commit", repo_root=repo)
    bug = _in_progress("bug", repo)

    with pytest.raises(RebarError) as excinfo:
        _close(bug, repo, caused_by=target)

    assert excinfo.value.returncode == 1
    message = str(excinfo.value)
    assert "no commit referencing" in message
    assert target in message
    assert "Nothing was written" in message
    assert rebar.show_ticket(bug, repo_root=repo)["status"] == "in_progress"
    assert _caused_by(bug, repo) == []


def test_link_refuses_the_same_target(rebar_repo: Path) -> None:
    """The close's refusal matches ``link``'s, for the same target in the same store."""
    repo = str(rebar_repo)
    target = rebar.create_ticket("task", "never shipped a commit", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)

    with pytest.raises(RebarError, match="no commit referencing"):
        rebar.link(bug, target, "caused_by", repo_root=repo)


def test_target_with_a_referencing_commit_is_recorded_explicit(rebar_repo: Path) -> None:
    repo = str(rebar_repo)
    target = rebar.create_ticket("task", "the culprit change", repo_root=repo)
    _commit(rebar_repo, f"the culprit change\n\nrebar-ticket: {target}")
    bug = _in_progress("bug", repo)

    _close(bug, repo, caused_by=target)

    [edge] = _caused_by(bug, repo)
    assert edge["target_id"] == target
    assert edge.get("provenance") == "explicit"


def test_unreadable_history_allows_the_edge(empty_repo: Path) -> None:
    """No commits at all: absence is unprovable, so the edge is allowed, as on ``link``."""
    repo = str(empty_repo)
    target = rebar.create_ticket("task", "unprovable", repo_root=repo)
    bug = _in_progress("bug", repo)

    _close(bug, repo, caused_by=target)

    assert [d["target_id"] for d in _caused_by(bug, repo)] == [target]


def test_already_active_target_is_not_rechecked(empty_repo: Path) -> None:
    """The close writes nothing for an active target, so it does not refuse over it."""
    repo = str(empty_repo)
    target = rebar.create_ticket("task", "linked while unprovable", repo_root=repo)
    bug = _in_progress("bug", repo)
    rebar.link(bug, target, "caused_by", repo_root=repo)
    _commit(empty_repo, "unrelated base commit")
    before = [(d["target_id"], d["link_uuid"]) for d in _caused_by(bug, repo)]

    _close(bug, repo, caused_by=target)

    assert rebar.show_ticket(bug, repo_root=repo)["status"] == "closed"
    assert [(d["target_id"], d["link_uuid"]) for d in _caused_by(bug, repo)] == before


def test_non_bug_close_is_not_refused(rebar_repo: Path) -> None:
    """A non-bug close records no ``caused_by`` edge, so the rule has nothing to guard."""
    repo = str(rebar_repo)
    target = rebar.create_ticket("task", "never shipped a commit", repo_root=repo)
    task = _in_progress("task", repo)

    _close(task, repo, caused_by=target, close_class="")

    assert rebar.show_ticket(task, repo_root=repo)["status"] == "closed"
    assert _caused_by(task, repo) == []


def test_blame_derived_culprit_is_not_rechecked(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank flag takes blame's culprit unchecked: blame names it from a commit's trailer."""
    repo = str(rebar_repo)
    target = rebar.create_ticket("task", "stand-in culprit", repo_root=repo)
    monkeypatch.setattr(
        "rebar.metrics.blame.derive_caused_by", lambda _bug, _root, _tracker: target
    )
    bug = _in_progress("bug", repo)

    _close(bug, repo)

    [edge] = _caused_by(bug, repo)
    assert edge["target_id"] == target
    assert edge.get("provenance") == "derived"
