"""WS-H: commits-on-ticket event type (COMMITS).

Pins: attach + read-back via replay; union-merge dedup by sha; HLC+UUID
convergence; lazy/additive (non-commit tickets unchanged); survives compaction;
fetch_commits reads it; and it is NOT a Jira-synced field (outbound differ
unaffected — `commits` is absent from the local→Jira field map).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import rebar
from rebar._commands import _seam


def _tracker(repo: Path) -> Path:
    return _seam.tracker_dir(str(repo))


def test_attach_and_read_back(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    out = rebar.attach_commits(
        tid, ["abc123", {"sha": "def456", "message": "fix"}], repo_root=str(rebar_repo)
    )
    assert out["attached"] == 2
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    shas = [c["sha"] for c in state["commits"]]
    assert shas == ["abc123", "def456"]
    assert state["commits"][1]["message"] == "fix"


def test_union_merge_dedups_by_sha(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    rebar.attach_commits(tid, ["a", "b"], repo_root=str(rebar_repo))
    rebar.attach_commits(tid, ["b", "c"], repo_root=str(rebar_repo))  # b is a dup
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert [c["sha"] for c in state["commits"]] == ["a", "b", "c"]


def test_non_commit_ticket_has_no_commits_key(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Plain", repo_root=str(rebar_repo))
    assert "commits" not in rebar.show_ticket(tid, repo_root=str(rebar_repo))


def test_convergence_replay_order(rebar_repo: Path) -> None:
    # Two raw COMMITS files written out of disk-order converge by filename (HLC+UUID).
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    env_id = (_tracker(rebar_repo) / ".env-id").read_text().strip()
    base = 1_781_000_000_000_000_000

    def write(ts, uid, sha):
        ev = {
            "timestamp": ts,
            "uuid": uid,
            "event_type": "COMMITS",
            "env_id": env_id,
            "author": "t",
            "data": {"commits": [sha]},
        }
        (_tracker(rebar_repo) / tid / f"{ts}-{uid}-COMMITS.json").write_text(json.dumps(ev))

    write(base + 5, "ffffffff-0000-4000-8000-000000000002", "second")
    write(base + 1, "ffffffff-0000-4000-8000-000000000001", "first")
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert [c["sha"] for c in state["commits"]] == ["first", "second"]  # filename order


def test_survives_compaction(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    rebar.attach_commits(tid, ["s1", "s2"], repo_root=str(rebar_repo))
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "compact", tid, "--threshold=0"],
        capture_output=True,
        text=True,
        cwd=str(rebar_repo),
        env={**os.environ, "REBAR_SYNC_PULL": "off"},
    )
    assert cp.returncode == 0, cp.stderr
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert [c["sha"] for c in state["commits"]] == ["s1", "s2"]


def test_fetch_commits_step_reads_it(rebar_repo: Path) -> None:
    from rebar.llm.workflow import steps
    from rebar.llm.workflow.executor import StepContext

    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    rebar.attach_commits(tid, ["x", "y"], repo_root=str(rebar_repo))
    ctx = StepContext(
        run_id="r",
        step_id="s",
        kind="scripted",
        step={},
        inputs={},
        workflow={},
        target_ticket=tid,
        repo_root=str(rebar_repo),
    )
    out = steps.fetch_commits(ctx)
    assert out["commit_count"] == 2
    assert [c["sha"] for c in out["commits"]] == ["x", "y"]


def test_commits_not_a_jira_synced_field() -> None:
    # The outbound differ is field-driven; `commits` must not be in the local->Jira
    # field map, so attaching commits never produces an outbound Jira change. The
    # engine dir ships as package DATA (not an in-process import), so read by path.
    differ = (
        Path(rebar.__file__).resolve().parent
        / "_engine"
        / "rebar_reconciler"
        / "outbound_differ.py"
    )
    src = differ.read_text(encoding="utf-8")
    assert '"commits"' not in src and "'commits'" not in src, (
        "commits leaked into the outbound differ — it must not be a Jira-synced field"
    )


def test_dedup_converges_under_reorder(rebar_repo: Path) -> None:
    # The SAME sha arriving in two events, written out of filename order, converges
    # to a single entry deterministically (union-add dedup by sha, replay order).
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    env_id = (_tracker(rebar_repo) / ".env-id").read_text().strip()
    base = 1_781_000_000_000_000_000

    def write(ts, uid, shas):
        ev = {
            "timestamp": ts,
            "uuid": uid,
            "event_type": "COMMITS",
            "env_id": env_id,
            "author": "t",
            "data": {"commits": shas},
        }
        (_tracker(rebar_repo) / tid / f"{ts}-{uid}-COMMITS.json").write_text(json.dumps(ev))

    write(base + 9, "ffffffff-0000-4000-8000-000000000002", ["dup", "later"])
    write(base + 1, "ffffffff-0000-4000-8000-000000000001", ["dup", "early"])
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    shas = [c["sha"] for c in state["commits"]]
    assert shas == ["dup", "early", "later"]  # first-occurrence-in-replay-order, deduped
