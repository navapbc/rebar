"""An unforced close judges its gate material from a freshly synced clone (bug 833e-3827).

MCP ``transition_ticket`` runs the library ``rebar.transition`` against the server's own
tickets clone. Read tools refresh that clone at most once a minute, and nothing on the close
path fetched, so a close could judge Acceptance Criteria boxes another clone had already
ticked (or unticked) and pushed. These tests drive that entry point against clone B while an
edit arrives on the shared origin from clone A, with B's read throttle marker fresh.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from _git_upkeep import init_bare_remote

import rebar
import rebar.llm
from rebar import config as cfg
from rebar._store import sync as _store_sync

UNTICKED = "Body.\n\n## Acceptance Criteria\n- [ ] first done\n- [ ] second done\n"
TICKED = "Body.\n\n## Acceptance Criteria\n- [x] first done\n- [x] second done\n"
ONE_UNTICKED = "Body.\n\n## Acceptance Criteria\n- [x] first done\n- [ ] second done\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _publish(tracker: Path) -> None:
    pushed = _git(tracker, "push", "-q", "origin", "HEAD:refs/heads/tickets")
    assert pushed.returncode == 0, pushed.stderr


def _stamp_fresh_marker(tracker: Path) -> None:
    """Record a read-time sync moments ago, as on a server other sessions keep reading."""
    md5 = hashlib.md5(os.path.realpath(str(tracker)).encode()).hexdigest()[:12]
    Path(f"/tmp/.ticket-sync-{md5}").write_text(str(int(time.time())))


def _pass(ticket_id, **kw):
    return {"verdict": "PASS", "findings": [], "runner": "fake", "model": "m"}


class _FetchSpy:
    """Counts fetch attempts made through the store sync seam."""

    def __init__(self, real) -> None:
        self._real = real
        self.fetches = 0

    def __call__(self, cwd, *args, **kwargs):
        if args and args[0] == "fetch":
            self.fetches += 1
        return self._real(cwd, *args, **kwargs)


@pytest.fixture
def clones(rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Clone A (the editor, ``rebar_repo``) and clone B (the gated "server") of one origin.

    A ticket exists in both at ``in_progress`` with an unticked description. The returned
    ``edit`` writes in A and publishes; ``close`` closes through B's library entry point.
    """
    monkeypatch.delenv("REBAR_SYNC_PULL", raising=False)
    a = rebar_repo
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "c"], cwd=a, check=True)
    origin = init_bare_remote(tmp_path / "origin.git", initial_branch="tickets")
    tracker_a = a / ".tickets-tracker"
    _git(tracker_a, "remote", "add", "origin", str(origin))
    tid = rebar.create_ticket("task", "fresh close", description=UNTICKED, repo_root=str(a))
    rebar.transition(tid, "open", "in_progress", repo_root=str(a))
    _publish(tracker_a)

    b = tmp_path / "server"
    subprocess.run(["git", "init", "-q", "-b", "main", str(b)], check=True)
    (b / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")
    _git(b, "add", "-A")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c"],
        cwd=b,
        check=True,
    )
    tracker_b = b / ".tickets-tracker"
    cloned = subprocess.run(
        ["git", "clone", "-q", "--branch", "tickets", str(origin), str(tracker_b)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cloned.returncode == 0, cloned.stderr
    _git(tracker_b, "config", "user.email", "t@t")
    _git(tracker_b, "config", "user.name", "t")
    (tracker_b / ".env-id").write_text(str(uuid.uuid4()))
    monkeypatch.setattr(rebar.llm, "verify_completion", _pass)

    def edit(description: str) -> None:
        rebar.edit_ticket(tid, description=description, repo_root=str(a))
        _publish(tracker_a)

    def use_server() -> None:
        monkeypatch.setenv("REBAR_ROOT", str(b))
        monkeypatch.chdir(b)
        cfg.reset_config_cache()

    def close(**kw) -> None:
        use_server()
        _stamp_fresh_marker(tracker_b)
        rebar.transition(tid, "in_progress", "closed", repo_root=str(b), **kw)

    def status() -> str:
        return rebar.show_ticket(tid, repo_root=str(b))["status"]

    return SimpleNamespace(
        tid=tid,
        origin=origin,
        tracker_b=tracker_b,
        edit=edit,
        use_server=use_server,
        close=close,
        status=status,
    )


def test_close_observes_boxes_ticked_in_another_clone(clones) -> None:
    clones.edit(TICKED)
    clones.close()
    assert clones.status() == "closed"


def test_close_refuses_a_box_unticked_in_another_clone(clones) -> None:
    """The unsatisfied direction: B already holds the ticked text, A unticks one box."""
    clones.edit(TICKED)
    clones.use_server()
    assert "- [ ]" not in rebar.show_ticket(clones.tid)["description"]  # B synced: ticked
    clones.edit(ONE_UNTICKED)
    with pytest.raises(rebar.RebarError) as ei:
        clones.close()
    assert ei.value.returncode == 1
    assert "- [ ] second done" in ei.value.stderr
    assert "first done" not in ei.value.stderr
    assert clones.status() == "in_progress"


def test_sync_pull_off_keeps_the_close_local(clones, monkeypatch) -> None:
    spy = _FetchSpy(_store_sync.run_git)
    monkeypatch.setattr(_store_sync, "run_git", spy)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    clones.edit(TICKED)
    with pytest.raises(rebar.RebarError) as ei:
        clones.close()
    assert spy.fetches == 0
    assert "- [ ] first done" in ei.value.stderr


def test_unreachable_origin_closes_on_local_state(clones, tmp_path) -> None:
    clones.edit(TICKED)
    _git(clones.tracker_b, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(rebar.RebarError) as ei:
        clones.close()
    assert "has unchecked Acceptance Criteria items" in ei.value.stderr
    assert clones.status() == "in_progress"


def test_idea_rejection_does_not_fetch(clones, monkeypatch) -> None:
    spy = _FetchSpy(_store_sync.run_git)
    monkeypatch.setattr(_store_sync, "run_git", spy)
    clones.use_server()
    idea = rebar.idea("reject me")
    _stamp_fresh_marker(clones.tracker_b)
    rebar.transition(idea, "idea", "closed")
    assert spy.fetches == 0


def test_forced_close_does_not_fetch(clones, monkeypatch) -> None:
    spy = _FetchSpy(_store_sync.run_git)
    monkeypatch.setattr(_store_sync, "run_git", spy)
    clones.close(force="operator judgement in a test")
    assert spy.fetches == 0
    assert clones.status() == "closed"
