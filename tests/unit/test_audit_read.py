"""Story 46f0: audit read layer — full-history readers + relates_to resolution + CLI/MCP.

`audit_trail(ticket_id)` aggregates a ticket's FULL retained plan-review history, its completion
attestation + sidecar record, and the associated code reviews (resolved via inbound `relates_to`
links from `code_review` tickets, each with its own sidecar history).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import rebar
from rebar.llm.code_review import sidecar as code_sidecar
from rebar.llm.plan_review import sidecar as plan_sidecar

pytestmark = pytest.mark.unit

_EXPECTED_KEYS = {"ticket", "plan_reviews", "completion", "code_reviews"}


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


def _plan_verdict(tid: str, text: str) -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": tid,
        "ticket_type": "task",
        "advisory": [{"id": "f1", "finding": text, "criteria": ["T1"], "decision": "advisory"}],
        "coverage": {"metrics": {}},
        "coaching": [],
    }


# ── AC1: the aggregator returns the exact typed-dict shape ──────────────────────────────────
def test_audit_trail_aggregates_full_history(store: Path) -> None:
    from rebar.audit.read import audit_trail

    r = str(store)
    tid = rebar.create_ticket("task", "work ticket", description="x" * 60, repo_root=r)

    # two plan-review sidecars (history, newest-first)
    assert plan_sidecar.emit(_plan_verdict(tid, "first"), material="m1", repo_root=r)
    assert plan_sidecar.emit(_plan_verdict(tid, "second"), material="m2", repo_root=r)

    # a completion PASS record
    from rebar.llm import completion_sidecar

    completion_sidecar.emit(
        {
            "verdict": "PASS",
            "ticket_id": tid,
            "findings": [],
            "criteria": [{"criterion": "AC1", "met": True, "kind": "codebase-verifiable"}],
            "runner": "fake",
        },
        repo_root=r,
    )

    # a code_review ticket linked relates_to the work ticket, with its own sidecar
    cr = rebar.create_ticket("code_review", f"code-review: {tid} @rev1", repo_root=r)
    rebar.link(cr, tid, "relates_to", repo_root=r)
    assert code_sidecar.emit(
        {"verdict": "PASS", "blocking": [], "advisory": [], "coaching": []},
        target_ticket=cr,
        repo_root=r,
    )

    trail = audit_trail(tid, repo_root=r)
    assert set(trail.keys()) == {"ticket", "plan_reviews", "completion", "code_reviews"}
    # plan history: both, newest-first
    assert isinstance(trail["plan_reviews"], list) and len(trail["plan_reviews"]) == 2
    assert trail["plan_reviews"][0]["material_fingerprint"] == "m2"  # newest first
    assert trail["plan_reviews"][1]["material_fingerprint"] == "m1"
    # completion is a CompletionRecord dict (a record exists): the seeded PASS sidecar is present
    assert isinstance(trail["completion"], dict)
    assert set(trail["completion"].keys()) == {"attestation", "sidecar"}
    assert trail["completion"]["sidecar"] is not None
    assert trail["completion"]["sidecar"]["verdict"] == "PASS"
    # code reviews resolved via relates_to, each with its own sidecar history
    assert isinstance(trail["code_reviews"], list) and len(trail["code_reviews"]) == 1
    entry = trail["code_reviews"][0]
    assert entry["ticket_id"] == cr
    assert isinstance(entry["sidecars"], list) and len(entry["sidecars"]) == 1


# ── AC2: the `rebar audit show … --output json` CLI surface ─────────────────────────────────
class _FakeMcp:
    """A minimal FastMCP stand-in: its ``.tool(...)`` decorator just captures the function."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, **_kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def test_audit_show_cli_json_shape(store: Path) -> None:
    """`rebar audit show <ticket> --output json` prints the AuditTrail dict as JSON to stdout."""
    r = str(store)
    tid = rebar.create_ticket("task", "cli work ticket", description="x" * 60, repo_root=r)
    assert plan_sidecar.emit(_plan_verdict(tid, "only"), material="m1", repo_root=r)

    env = dict(os.environ)
    env["REBAR_ROOT"] = r
    env["REBAR_SIGNING_KEY"] = "k"
    proc = subprocess.run(
        [sys.executable, "-m", "rebar", "audit", "show", tid, "--output", "json"],
        cwd=r,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    data = json.loads(proc.stdout)
    assert set(data.keys()) == _EXPECTED_KEYS
    assert data["ticket"]["ticket_id"] == tid
    assert isinstance(data["plan_reviews"], list) and len(data["plan_reviews"]) == 1


# ── AC3: the `audit_trail` MCP read tool (served even under REBAR_MCP_READONLY=1) ────────────
def _register_audit_tool():
    from rebar import _mcp_reads

    m = _FakeMcp()
    ctx = types.SimpleNamespace(
        readonly=True,
        allow_jira_sync=False,
        cap_workflow_payload=lambda *a, **k: None,
        MODE_CAPS={},
        Mode=None,
    )
    _mcp_reads.register_read_tools(m, ctx=ctx)
    return m.tools["audit_trail"]


def test_audit_trail_mcp_read_tool(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The registered ``audit_trail`` read tool returns the AuditTrail shape under readonly."""
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    r = str(store)
    tid = rebar.create_ticket("task", "mcp work ticket", description="x" * 60, repo_root=r)
    assert plan_sidecar.emit(_plan_verdict(tid, "only"), material="m1", repo_root=r)

    tool = _register_audit_tool()
    result = tool(tid)
    assert set(result.keys()) == _EXPECTED_KEYS
    assert result["ticket"]["ticket_id"] == tid
    assert isinstance(result["plan_reviews"], list) and len(result["plan_reviews"]) == 1


# ── HELD-OUT edge tests (restored) ──
def test_audit_trail_bare_ticket_yields_empty(store: Path) -> None:
    """A ticket with no reviews: plan_reviews == [], completion is None, code_reviews == []."""
    from rebar.audit.read import audit_trail

    r = str(store)
    tid = rebar.create_ticket("task", "bare", description="y" * 60, repo_root=r)
    trail = audit_trail(tid, repo_root=r)
    assert set(trail.keys()) == {"ticket", "plan_reviews", "completion", "code_reviews"}
    assert trail["plan_reviews"] == []
    assert trail["completion"] is None
    assert trail["code_reviews"] == []


# ── the history enumerators (held-out) ──────────────────────────────────────────────────────
def test_all_review_results_returns_full_history_newest_first(store: Path) -> None:
    r = str(store)
    tid = rebar.create_ticket("task", "hist", description="z" * 60, repo_root=r)
    for i in range(3):
        assert plan_sidecar.emit(_plan_verdict(tid, f"f{i}"), material=f"m{i}", repo_root=r)
    hist = plan_sidecar.all_review_results(tid, repo_root=r)
    assert [h["material_fingerprint"] for h in hist] == ["m2", "m1", "m0"]  # newest-first
    assert plan_sidecar.all_review_results("nonexistent", repo_root=r) == []
