"""Inline-commit writes auto-push on their own (bug prone-octet-cheek).

``transition`` / ``reopen`` / ``claim`` (txn.py), ``compact`` (compact.py), and
``delete`` (delete.py) do their own locked rename+commit instead of going through
``write_and_push``. The auto-push must still fire for each, otherwise a trailing
status/compact/delete — the LAST write of a session, e.g. closing an epic — leaves
its commit stranded as PUSH_PENDING (origin/tickets behind local).

These pin the observable git effect against a real local bare origin: after each
such write, with NO following append_event write to "carry" it, the local tickets
branch must be EVEN with origin/tickets (ahead == 0). The default push policy
(``always``) is in force (the push-policy matrix is covered by
test_push_policy_e2e.py).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True, text=True
    )
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t.co", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    yield repo


def _ahead(repo: Path) -> int:
    """How many commits the local tickets branch is ahead of origin/tickets."""
    tracker = repo / ".tickets-tracker"
    subprocess.run(
        ["git", "fetch", "-q", "origin", "tickets"], cwd=tracker, capture_output=True, text=True
    )
    out = subprocess.run(
        ["git", "rev-list", "--count", "FETCH_HEAD..HEAD"],
        cwd=tracker,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip() or "0")


def _ac(n: str) -> str:
    return f"Body for {n}.\n\n## Acceptance Criteria\n- [ ] x"


def test_claim_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    assert _ahead(repo) == 0  # create pushed
    rebar.claim(t, assignee="me", repo_root=str(repo))
    assert _ahead(repo) == 0, "claim's STATUS commit must reach origin without a carrying write"


def test_transition_close_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    rebar.claim(t, assignee="me", repo_root=str(repo))
    rebar.transition(t, "in_progress", "closed", repo_root=str(repo))
    # close writes STATUS + a compact-on-close SNAPSHOT, both inline-committed.
    assert _ahead(repo) == 0, "a trailing close must not strand its STATUS/SNAPSHOT (PUSH_PENDING)"


def test_reopen_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    rebar.transition(t, "open", "closed", repo_root=str(repo))
    rebar.reopen(t, repo_root=str(repo))
    assert _ahead(repo) == 0, "reopen (a transition) must push on its own"


def test_compact_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    from rebar._commands import compact

    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    for i in range(3):
        rebar.comment(t, f"comment {i}", repo_root=str(repo))
    assert _ahead(repo) == 0
    rc = compact.compact_cli([t, "--threshold=0"], repo_root=str(repo))
    assert rc == 0
    assert _ahead(repo) == 0, "a standalone compact's SNAPSHOT commit must reach origin"


def test_delete_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    from rebar._commands import delete

    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    assert _ahead(repo) == 0
    rc = delete.delete_cli([t, "--user-approved"], repo_root=str(repo))
    assert rc == 0
    assert _ahead(repo) == 0, "a trailing delete must not strand its DELETE commit"
