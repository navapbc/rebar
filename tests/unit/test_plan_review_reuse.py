"""BLOCK verdict reuse (bug 7e77 — masterful-whimsical-tigermoth).

``_idempotent_reuse`` (feature b3e5) covers only PASS: it requires a certified
attestation, and a BLOCK never signs. So re-running ``review-plan`` on an UNCHANGED
blocked plan re-paid the full multi-pass LLM review every time. The fix adds a
``verdict_reuse`` path beside it: when no attestation applies, the stored BLOCK
verdict in the latest ``REVIEW_RESULT`` sidecar is reused — zero LLM calls — when
its ``material_fingerprint`` AND ``verified_at_sha`` both still match the current
plan/code, and ``--force`` bypasses it. A reused BLOCK exits 1, renders the stored
findings with a "reused" marker, and emits NO new sidecar.

Offline end-to-end: a DET-floor BLOCK (missing Acceptance Criteria fails P1
unconditionally) with a schema-aware counting fake runner, so no model/network —
mirrors tests/interfaces/lifecycle/test_block_loop_remediation.py.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar.llm.plan_review import sidecar
from rebar.llm.runner import FakeRunner


class _CountingGateFake(FakeRunner):
    """Shape-valid offline runner for the plan-review passes (mirrors the lifecycle gate
    tests' fake) that COUNTS invocations — the oracle for "reuse ran zero LLM calls"."""

    name = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run(self, req) -> dict:  # type: ignore[override]
        from rebar.llm import findings as _f

        self.calls += 1
        schema = req.output_schema
        if req.mode == "text":
            return {"text": "[fake summary]", "runner": self.name, "model": None, "trace_id": None}
        if schema == "plan_review_verification":
            idxs = [int(x) for x in re.findall(r"finding index (\d+)", req.instructions or "")]
            payload = {"verifications": [{"index": i} for i in idxs]}
        elif schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


# No "## Acceptance Criteria" block → the DET floor's P1 readiness-shape check BLOCKS
# unconditionally, so every review of this plan is a deterministic BLOCK.
_BLOCKING_DESC = (
    "A long-enough plan body that still fails the deterministic readiness floor because it "
    "carries no Acceptance Criteria checklist at all, exercising the unrevised-BLOCK-retry "
    "regime the verdict-reuse path exists for.\n\n## What\nchange a thing\n## Why\nbecause\n"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialized rebar repo in a temp git dir (mirrors the interfaces-tier
    ``rebar_repo`` fixture; local so the unit tier stays self-contained)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    # Give the CODE branch a root commit so the suite-wide gate default can resolve a ref.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _make_blocked(repo: Path) -> str:
    return rebar.create_ticket(
        "task", "blocked plan", description=_BLOCKING_DESC, repo_root=str(repo)
    )


def _sidecar_count(repo: Path, tid: str) -> int:
    ticket_dir = repo / ".tickets-tracker" / tid
    return len(list(ticket_dir.glob(f"*-{sidecar.EVENT_TYPE}.json")))


# ── AC1: an unchanged BLOCK is reused with ZERO LLM calls ───────────────────────
def test_unchanged_block_reuses_verdict_with_zero_llm_calls(repo: Path) -> None:
    tid = _make_blocked(repo)
    runner = _CountingGateFake()

    v1 = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    assert v1["verdict"] == "BLOCK"
    assert v1["blocking"]  # the DET P1 block is a stored, surfaced finding
    first_calls = runner.calls
    sidecars_after_first = _sidecar_count(repo, tid)
    assert sidecars_after_first >= 1  # the first review persisted its REVIEW_RESULT

    v2 = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    assert runner.calls == first_calls  # ZERO further LLM calls — the runner never ran
    assert v2["verdict"] == "BLOCK"
    assert v2["coverage"]["verdict_reuse"] is True
    assert v2["coverage"]["llm_ran"] is False
    assert v2["runner"] == "reused"
    # The stored blocking findings are rendered back, not recomputed.
    assert [f.get("id") for f in v2["blocking"]] == [f.get("id") for f in v1["blocking"]]
    # No NEW sidecar: reuse is a read, not a review.
    assert v2["sidecar_emitted"] is False
    assert _sidecar_count(repo, tid) == sidecars_after_first
    # A reused BLOCK is still unsigned (a BLOCK never signs).
    assert v2["signature"]["signed"] is False


# ── AC2: a reused BLOCK exits 1 and renders a "reused" marker ───────────────────
def test_reused_block_exits_1(repo: Path) -> None:
    from rebar._cli._llm_commands import _disposition_exit_code

    tid = _make_blocked(repo)
    runner = _CountingGateFake()
    rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    v2 = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    assert v2["coverage"]["verdict_reuse"] is True
    # The CLI maps a reused BLOCK to the same exit the fresh BLOCK produced.
    assert _disposition_exit_code(v2, indeterminate_code=2) == 1


def test_render_plan_review_text_marks_verdict_reuse(capsys) -> None:
    from rebar._cli._llm_commands import _render_plan_review_text

    _render_plan_review_text(
        {
            "verdict": "BLOCK",
            "ticket_id": "t",
            "runner": "reused",
            "coverage": {"llm_ran": False, "verdict_reuse": True, "counts": {}},
            "blocking": [{"criteria": ["P1"], "finding": "no acceptance criteria"}],
            "advisory": [],
            "coaching": [],
        }
    )
    out = capsys.readouterr().out
    assert "BLOCK" in out and "reused" in out
    assert "no acceptance criteria" in out  # the STORED finding is rendered back


def test_render_plan_review_text_fresh_block_has_no_reuse_marker(capsys) -> None:
    from rebar._cli._llm_commands import _render_plan_review_text

    _render_plan_review_text(
        {
            "verdict": "BLOCK",
            "ticket_id": "t",
            "runner": "fake",
            "coverage": {"llm_ran": True, "counts": {}},
            "blocking": [{"criteria": ["P1"], "finding": "no acceptance criteria"}],
            "advisory": [],
            "coaching": [],
        }
    )
    assert "reused" not in capsys.readouterr().out


# ── AC3: --force bypasses verdict reuse ─────────────────────────────────────────
def test_force_bypasses_verdict_reuse(repo: Path) -> None:
    tid = _make_blocked(repo)
    runner = _CountingGateFake()
    rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    first_calls = runner.calls

    v2 = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), force=True)
    assert runner.calls > first_calls  # the LLM ran again under --force
    assert v2["coverage"].get("verdict_reuse") is not True


# ── the reuse preconditions: plan OR code changed → full review ─────────────────
def test_material_change_reruns_full_review(repo: Path) -> None:
    tid = _make_blocked(repo)
    runner = _CountingGateFake()
    rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    first_calls = runner.calls

    rebar.edit_ticket(
        tid,
        description=_BLOCKING_DESC + "\nAn edited paragraph — the remediation attempt.\n",
        repo_root=str(repo),
    )
    v2 = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    assert runner.calls > first_calls  # fingerprint changed → full re-review
    assert v2["coverage"].get("verdict_reuse") is not True


def test_code_change_reruns_full_review(repo: Path) -> None:
    tid = _make_blocked(repo)
    runner = _CountingGateFake()
    rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    first_calls = runner.calls

    # Advance the reviewed code HEAD → verified_at_sha no longer matches.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "drift"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    v2 = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    assert runner.calls > first_calls  # code drifted → full re-review
    assert v2["coverage"].get("verdict_reuse") is not True
