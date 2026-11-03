"""The marker refusal is a property of the grammar, not of the completion gate (d68f-4771).

Placed where it was first written, the refusal sat past the gate-skip return and so fired
only in a project that happened to have the completion-verification close gate ENABLED.
That made the admission's expiry conditional on an unrelated setting: a project with the
gate off still carries markers, and an admission discharged only when some other gate is
on is not discharged at all. These tests pin the refusal to the close itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import gates, transition

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _all_gates_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gates, "gate_enabled", lambda _root, _name, **_k: False)


def _mark(repo: Path, ticket: str) -> None:
    """Commit a TRACKED file carrying a marker whose sunset is ``ticket``."""
    (repo / "mod.py").write_text(
        f"x = 1\n# mechanism-ok: lock probe.lock — {ticket} bounded by its sunset\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "mod.py"], check=True, capture_output=True)


def test_an_open_marker_refuses_the_close_with_every_gate_off(store, monkeypatch):
    """The regression. Gate state must not decide whether an admission expires."""
    _all_gates_off(monkeypatch)
    tid = rebar.create_ticket("task", "retires the probe lock", repo_root=str(store))
    rebar.claim(tid, repo_root=str(store))
    _mark(store, tid)

    assert transition.transition_cli([tid, "in_progress", "closed"]) != 0


def test_a_disposition_close_does_not_slip_past_the_marker(store, monkeypatch):
    """A disposition attests why completion is not claimed — not that the marker is gone.

    Dispositions return before the verifier, so a refusal placed any later than this one
    would be skipped by every administrative close as well as by a gate-off project.
    """
    _all_gates_off(monkeypatch)
    canonical = rebar.create_ticket("task", "canonical", repo_root=str(store))
    tid = rebar.create_ticket("task", "the copy", repo_root=str(store))
    rebar.claim(tid, repo_root=str(store))
    rebar.link(tid, canonical, "duplicates", repo_root=str(store))
    _mark(store, tid)

    assert transition.transition_cli([tid, "in_progress", "closed", "--class=duplicate"]) != 0


def test_removing_the_marker_lets_the_same_close_through(store, monkeypatch):
    """The refusal is the marker's, so clearing the marker clears the refusal.

    Without this the two tests above would also pass if the close were broken outright.
    """
    _all_gates_off(monkeypatch)
    tid = rebar.create_ticket("task", "retires the probe lock", repo_root=str(store))
    rebar.claim(tid, repo_root=str(store))
    _mark(store, tid)
    assert transition.transition_cli([tid, "in_progress", "closed"]) != 0

    (store / "mod.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(store), "add", "mod.py"], check=True, capture_output=True)
    assert transition.transition_cli([tid, "in_progress", "closed"]) == 0
