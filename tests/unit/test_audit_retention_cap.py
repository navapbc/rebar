"""Story fde0: raise the review-sidecar retention cap (10 -> 50) as one shared constant, and
add the previously-absent code-review prune path governed by that same constant.
"""

from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config as _config
from rebar._commands._seam import append_event
from rebar.llm.code_review import sidecar as code_sidecar
from rebar.llm.plan_review import sidecar as plan_sidecar

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
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _events_in(repo: Path, tid: str) -> list[str]:
    tracker = str(_config.tracker_dir(str(repo)))
    from rebar._engine_support.resolver import resolve_ticket_id

    rid = resolve_ticket_id(tid, tracker) or tid
    d = os.path.join(tracker, rid)
    return sorted(f for f in os.listdir(d) if f.endswith("-REVIEW_RESULT.json"))


# ── the cap is 50, a SINGLE definition shared by both paths ────────────────────────────────
def test_retention_cap_is_50_and_a_single_shared_definition() -> None:
    assert plan_sidecar.RETAIN_PER_TICKET == 50
    # code-review does not define its own literal — it obtains the SAME value (== 50) by import
    assert code_sidecar.RETAIN_PER_TICKET == 50
    assert code_sidecar.RETAIN_PER_TICKET == plan_sidecar.RETAIN_PER_TICKET
    # both prune functions default `keep` to that constant
    assert inspect.signature(plan_sidecar.prune).parameters["keep"].default == 50
    assert inspect.signature(code_sidecar.prune).parameters["keep"].default == 50


# ── the previously-absent code-review prune bounds retention, newest-first ─────────────────
def test_code_review_prune_keeps_newest(store: Path) -> None:
    tid = rebar.create_ticket("code_review", "code-review: session:s", repo_root=str(store))
    tracker = _config.tracker_dir(str(store))
    for i in range(6):
        append_event(
            tid,
            "REVIEW_RESULT",
            {"schema": "code_review_result_v2", "verdict": "PASS", "ticket_id": tid, "seq": i},
            tracker,
            repo_root=str(store),
        )
    assert len(_events_in(store, tid)) == 6
    removed = code_sidecar.prune(tid, keep=3, repo_root=str(store))
    assert removed == 3
    remaining = _events_in(store, tid)
    assert len(remaining) == 3  # the 3 newest (highest ns-timestamp filename prefix) are kept


# ── emit is APPEND-ONLY: exceeding the cap must NOT delete committed events ────────────────
def test_code_review_emit_keeps_every_event_past_the_cap(store: Path) -> None:
    """Emitting more than RETAIN_PER_TICKET review results on one target ticket retains ALL of
    them. Physically deleting a committed UUID event breaks store invariant I1 (append-only):
    delete(oldest)+add(new-uuid) in two clones reads to Git as a rename/rename conflict, which
    wedges reconvergence. Retention is bounded only by an explicit operator-invoked prune."""
    tid = rebar.create_ticket("code_review", "code-review: session:cap", repo_root=str(store))
    n = code_sidecar.RETAIN_PER_TICKET + 3
    for _ in range(n):
        assert code_sidecar.emit(
            {"verdict": "PASS", "blocking": [], "advisory": [], "coaching": []},
            target_ticket=tid,
            repo_root=str(store),
        )
    assert len(_events_in(store, tid)) == n


# ── emit must NOT auto-invoke prune: pruning is operator-invoked, never a write-path effect ─
def test_code_review_emit_does_not_call_prune(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(code_sidecar, "prune", lambda tid, **kw: calls.append(tid))
    tid = rebar.create_ticket("code_review", "code-review: session:e", repo_root=str(store))
    verdict = {"verdict": "PASS", "advisory": [], "blocking": [], "coaching": []}
    assert code_sidecar.emit(verdict, target_ticket=tid, repo_root=str(store))
    assert calls == [], "emit must append only; prune is an explicit operator-invoked operation"
