"""The dependency-advisory lanes are WIRED, not just implemented (bug 63e8).

``scripts/dependency_audit.py`` can be perfectly correct and still fix nothing if a
workflow never calls it, calls it with the wrong lane, or calls it from a job the
publishing jobs do not descend from. Workflow YAML is not reachable from a unit test by
execution, so these tests parse it: they are the only thing standing between a correct
module and a gate that silently does not run — the failure mode a sibling investigation
(ticket 599e) found in a different test surface.

Claims pinned here:

* the per-change lane resolves ``gerrit`` on the Verified lane and ``branch`` otherwise;
* the release gate runs the ``release`` lane in ``authorize``, which every publishing job
  reaches through ``needs:``, so a blocking advisory stops the release before any build;
* the scheduled canary exists, is scheduled, and escalates through ``advisory-alert``;
* the old unconditional shell gate is gone (a leftover copy would re-break the lane split).
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[2]
_BAT = _ROOT / ".github" / "workflows" / "_build-and-test.yml"
_RELEASE = _ROOT / ".github" / "workflows" / "release.yml"
_CANARY = _ROOT / ".github" / "workflows" / "dependency-advisory-canary.yml"
_RUNBOOK = _ROOT / "docs" / "dependency-advisory-runbook.md"

_GATE_CMD = "scripts/dependency_audit.py gate"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _run_blocks(job: dict) -> list[str]:
    return [str(step.get("run", "")) for step in job.get("steps", [])]


def _step_texts(job: dict) -> str:
    """Every step's run block plus its env values, concatenated."""
    parts: list[str] = []
    for step in job.get("steps", []):
        parts.append(str(step.get("run", "")))
        for value in (step.get("env") or {}).values():
            parts.append(str(value))
    return "\n".join(parts)


# ── The per-change lane (_build-and-test.yml) ───────────────────────────────────────


def test_pip_audit_job_calls_the_lane_gate() -> None:
    job = _load(_BAT)["jobs"]["pip-audit"]
    assert any(_GATE_CMD in block for block in _run_blocks(job)), (
        "the pip-audit job must delegate its verdict to scripts/dependency_audit.py"
    )


def test_lane_is_gerrit_only_when_a_gerrit_refspec_is_present() -> None:
    """The refspec is the ONLY thing that distinguishes 'this change owns it'."""
    text = _step_texts(_load(_BAT)["jobs"]["pip-audit"])
    assert "inputs.gerrit-refspec != ''" in text and "'gerrit'" in text and "'branch'" in text
    assert '--lane "$LANE"' in text


def test_the_old_unconditional_shell_gate_is_gone() -> None:
    """A leftover copy of the always-fail loop would re-break the lane split."""
    body = _BAT.read_text(encoding="utf-8")
    assert "pip-audit found dependency vulnerabilities" not in body
    assert "if out=$(pip-audit --desc" not in body


def test_gerrit_lane_deepens_the_checkout_so_the_diff_is_resolvable() -> None:
    """Without the parent commit the gate fails closed and blocks everything."""
    text = _step_texts(_load(_BAT)["jobs"]["pip-audit"])
    assert "--deepen=1" in text


# ── The release gate (release.yml) ─────────────────────────────────────────────────


def test_release_authorize_runs_the_release_lane_gate() -> None:
    job = _load(_RELEASE)["jobs"]["authorize"]
    assert any(f"{_GATE_CMD} --lane release" in block for block in _run_blocks(job)), (
        "release.yml's authorize job must gate on outstanding advisories"
    )


def test_every_publishing_job_descends_from_authorize() -> None:
    """The gate is only a gate if publishing cannot route around it."""
    jobs = _load(_RELEASE)["jobs"]

    def reaches_authorize(name: str, seen: set[str]) -> bool:
        if name == "authorize":
            return True
        if name in seen:
            return False
        seen.add(name)
        needs = jobs.get(name, {}).get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        return any(reaches_authorize(dep, seen) for dep in needs)

    for publishing in ("build", "publish", "github_release", "mcp_registry"):
        assert publishing in jobs, f"expected release job {publishing!r}"
        assert reaches_authorize(publishing, set()), (
            f"{publishing} does not descend from authorize — the advisory gate is bypassable"
        )


# ── The scheduled escalation lane ──────────────────────────────────────────────────


def test_canary_is_scheduled() -> None:
    workflow = _load(_CANARY)
    # PyYAML parses the bare key `on:` as the boolean True.
    triggers = workflow.get("on") or workflow.get(True)
    assert triggers.get("schedule"), "the advisory lane must be scheduled"


def test_canary_audits_and_escalates() -> None:
    job = _load(_CANARY)["jobs"]["advisory"]
    blocks = _run_blocks(job)
    assert any(f"{_GATE_CMD} --lane branch" in block for block in blocks)
    assert any("dependency_audit.py advisory-alert" in block for block in blocks)


def test_canary_escalation_survives_a_blocking_verdict() -> None:
    """A blocking gate must not skip the ticket step — the ticket IS the escalation."""
    steps = _load(_CANARY)["jobs"]["advisory"]["steps"]
    audit = next(s for s in steps if _GATE_CMD in str(s.get("run", "")))
    assert audit.get("continue-on-error") is True
    assert audit.get("id") == "audit"


def test_canary_can_write_tickets() -> None:
    workflow = _load(_CANARY)
    assert workflow["permissions"]["contents"] == "write"


# ── The runbook is discoverable from the failure ───────────────────────────────────


def test_runbook_exists_and_is_indexed() -> None:
    assert _RUNBOOK.is_file()
    index = (_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "dependency-advisory-runbook.md" in index


def test_failure_output_points_at_the_runbook() -> None:
    """A person hitting the failure must find the runbook without knowing it exists."""
    module = (_ROOT / "scripts" / "dependency_audit.py").read_text(encoding="utf-8")
    assert "docs/dependency-advisory-runbook.md" in module


@pytest.mark.parametrize(
    "phrase",
    [
        "override-dependencies",
        "constraint-dependencies",
        "UNSATISFIABLE",
        "NO-OP against an upstream cap",
        "REMOVE-WHEN",
        "not published in wheel metadata",
        "uv lock --check",
        "uv sync --locked --extra dev",
    ],
)
def test_runbook_banks_the_hard_won_uv_knowledge(phrase: str) -> None:
    """The knowledge that must not be rediscovered under time pressure (bug 63e8)."""
    body = _RUNBOOK.read_text(encoding="utf-8")
    assert phrase.lower() in body.lower(), f"runbook lost: {phrase}"
