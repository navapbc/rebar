"""Shared seeding helpers for the audit per-ticket page tests (story ff6f).

Builds a real rebar store and emits rich plan-review / completion / code-review
sidecars so the ``/ticket/<id>`` page has genuine audit data to render. Kept out
of a ``test_`` module so both the happy-path and held-out suites can import it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import rebar
from rebar.llm.plan_review import sidecar as plan_sidecar


def make_store(tmp_path: Path, monkeypatch) -> Path:
    """A real, initialised rebar store rooted at ``tmp_path/repo``."""
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


def finding(
    fid: str,
    *,
    decision: str,
    finding: str,
    priority: float,
    block_threshold: float,
    criteria=("T1",),
    location: str = "src/x.py:1",
    evidence=("evidence-text",),
    scenarios=("scenario-text",),
    verification=None,
    reason: str = "reason-text",
    suggested_fix: str = "fix-text",
) -> dict:
    """Build one plan-review finding carrying the full v2 field set."""
    return {
        "id": fid,
        "finding": finding,
        "criteria": list(criteria),
        "location": location,
        "decision": decision,
        "priority": priority,
        "block_threshold": block_threshold,
        "blocking_enabled": True,
        "evidence": list(evidence),
        "scenarios": list(scenarios),
        "verification": verification or {"binary": {"is_real": "yes"}},
        "reason": reason,
        "suggested_fix": suggested_fix,
        "impact": 0.5,
    }


def emit_plan_round(
    repo: str,
    tid: str,
    *,
    findings: list[dict],
    coaching: list[dict] | None = None,
    verdict: str = "FAIL",
    model: str = "claude-x",
    material: str = "m1",
) -> None:
    """Emit one plan-review round. ``findings`` are bucketed by their ``decision``
    into the verdict's ``blocking``/``advisory``/``dropped``/``indeterminate``
    lists (the emit path pools them back into ``findings``)."""
    buckets: dict[str, list[dict]] = {
        "block": [],
        "advisory": [],
        "dropped": [],
        "indeterminate": [],
    }
    for f in findings:
        buckets.setdefault(f["decision"], buckets["advisory"]).append(f)
    payload = {
        "verdict": verdict,
        "ticket_id": tid,
        "ticket_type": "task",
        "blocking": buckets["block"],
        "advisory": buckets["advisory"],
        "dropped": buckets["dropped"],
        "indeterminate": buckets["indeterminate"],
        "coverage": {"metrics": {}},
        "coaching": coaching or [],
        "model": model,
        "impact_model_version": "plan-v2",
        "material_fingerprint": material,
    }
    assert plan_sidecar.emit(payload, material=material, repo_root=repo)


def emit_completion_pass(repo: str, tid: str, criteria: list[dict]) -> None:
    """Emit a PASS completion verdict carrying ``criteria[]``."""
    from rebar.llm import completion_sidecar

    completion_sidecar.emit(
        {
            "verdict": "PASS",
            "ticket_id": tid,
            "findings": [],
            "criteria": criteria,
            "runner": "fake",
        },
        repo_root=repo,
    )


def emit_completion_fail(repo: str, tid: str, findings: list[dict]) -> None:
    """Emit a FAIL completion verdict (failures-only ``findings``, no ``criteria``)."""
    from rebar.llm import completion_sidecar

    completion_sidecar.emit(
        {
            "verdict": "FAIL",
            "ticket_id": tid,
            "findings": findings,
            "runner": "fake",
        },
        repo_root=repo,
    )


def emit_code_review(repo: str, tid: str, *, blocking=(), advisory=()) -> str:
    """Create a code_review artifact ticket linked to ``tid`` and emit a sidecar."""
    from rebar.llm.code_review import sidecar as code_sidecar

    cr = rebar.create_ticket("code_review", f"code-review: {tid} @rev1", repo_root=repo)
    rebar.link(cr, tid, "relates_to", repo_root=repo)
    assert code_sidecar.emit(
        {
            "verdict": "FAIL" if blocking else "PASS",
            "blocking": list(blocking),
            "advisory": list(advisory),
            "coaching": [],
        },
        target_ticket=cr,
        repo_root=repo,
    )
    return cr


def emit_code_round(repo: str, cr: str, *, blocking=(), advisory=()) -> None:
    """Emit an ADDITIONAL code-review round (sidecar) onto an existing artifact ``cr``,
    so one code_review ticket carries multiple retained rounds (newest-first)."""
    from rebar.llm.code_review import sidecar as code_sidecar

    assert code_sidecar.emit(
        {
            "verdict": "FAIL" if blocking else "PASS",
            "blocking": list(blocking),
            "advisory": list(advisory),
            "coaching": [],
        },
        target_ticket=cr,
        repo_root=repo,
    )
