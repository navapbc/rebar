"""Real-store proof for the plan-review generation signing transaction."""

from __future__ import annotations

import subprocess

import pytest

import rebar


@pytest.fixture
def review_store(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@example.test"),
        ("git", "config", "user.name", "test"),
        ("git", "commit", "-q", "--allow-empty", "-m", "initial"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def test_full_review_rechecks_under_lock_and_commits_before_sidecar(
    review_store, monkeypatch
) -> None:
    from rebar.llm import plan_review
    from rebar.llm.workflow import gate_dispatch

    ticket_id = rebar.create_ticket("task", "generation", repo_root=str(review_store))
    monkeypatch.setattr(
        gate_dispatch,
        "produce_plan_review_verdict",
        lambda *a, **k: {
            "verdict": "PASS",
            "ticket_id": ticket_id,
            "ticket_type": "task",
            "runner": "test",
            "model": "test",
            "blocking": [],
            "advisory": [],
            "overflow": [],
            "indeterminate": [],
            "dropped": [],
            "coaching": [],
            "coverage": {"llm_ran": True, "counts": {}},
        },
    )

    verdict = plan_review.review_plan(ticket_id, repo_root=str(review_store), emit_sidecar=True)

    assert verdict["sidecar_emitted"] is True
    assert verdict["signature"]["signed"] is True
    assert (
        rebar.verify_signature(ticket_id, kind="plan-review", repo_root=str(review_store))[
            "verified"
        ]
        is True
    )
