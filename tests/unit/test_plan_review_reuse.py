"""Unchanged BLOCK verdicts reuse stored findings without a new model run, although BLOCK has no
attestation. Material or review-code changes invalidate reuse. PASS reuse follows its signed
path. Tests count calls and retain the blocked result."""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar.llm.plan_review import sidecar
from rebar.llm.review_kernel import decide as kdecide
from rebar.llm.runner import FakeRunner

# The blocking LLM criterion the fake pins its finding to: F1 has default_posture
# "blocking" at block_threshold 0.60 in the packaged routing, routes 1-TURN, and
# applies to a leaf task — so a verified full-yes finding on it decides "block".
_BLOCK_CRITERION = "F1"


class _CountingGateFake(FakeRunner):
    """Offline plan-review runner that counts calls and returns a BLOCK for the selected finder
    chunk."""

    name = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run(self, req) -> dict:  # type: ignore[override]
        from rebar.llm import findings as _f

        self.calls += 1
        schema = req.output_schema
        instructions = req.instructions or ""
        if req.mode == "text":
            return {"text": "[fake summary]", "runner": self.name, "model": None, "trace_id": None}
        if schema == "plan_review_verification":
            idxs = [int(x) for x in re.findall(r"finding index (\d+)", instructions)]
            payload = {
                "verifications": [
                    {
                        "index": i,
                        "binary": {
                            **{q: "yes" for q in kdecide.GRADED_BINARY},
                            "cited_reference_accurate": "na",
                        },
                        "severity_attributes": {"vague_directive": "high"},
                    }
                    for i in idxs
                ]
            }
        elif schema == "plan_review_coach":
            payload = {"notes": []}
        elif schema == "plan_review_findings":
            # Pass-1 keeps only findings whose criteria are in the CHUNK's id set
            # (out-of-set findings are dropped, never re-attributed) — so emit the
            # blocking finding only in the chunk that carries _BLOCK_CRITERION.
            m = re.search(r"\(ids: ([^)]*)\)", instructions)
            ids = [s.strip() for s in (m.group(1).split(",") if m else [])]
            found = (
                [
                    {
                        "finding": "The stated change is too vague to execute as written.",
                        "criteria": [_BLOCK_CRITERION],
                        "location": "## What",
                        "evidence": ["change a thing in `src/thing.py`."],
                        "impact": "An executor cannot tell what done looks like.",
                        "suggested_fix": "Name the exact behavior change and its call sites.",
                    }
                ]
                if _BLOCK_CRITERION in ids
                else []
            )
            payload = {"analysis": "", "findings": found}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


# The plan PASSES the DET floor (proper `## Acceptance Criteria` checklist, no sentinel
# values) so the multi-pass LLM review actually runs; the BLOCK comes from the fake's
# F1 finding above. A DET-blocked plan would short-circuit at zero LLM calls (228b).
_BLOCKING_DESC = (
    "A plan body that clears the deterministic readiness floor so the LLM tier runs, "
    "exercising the unrevised-BLOCK-retry regime the verdict-reuse path exists for.\n\n"
    "## What\nchange a thing in `src/thing.py`.\n\n"
    "## Why\nbecause the current behavior is wrong.\n\n"
    "## Acceptance Criteria\n"
    "- [ ] the thing is observably changed\n"
    "- [ ] `pytest tests/unit` proves the change\n"
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
    # Ambient-env isolation (xdist worksteal moves tests between workers, so every test
    # must be self-contained): an operator-set REBAR_USAGE_LOG would make all tests append
    # to ONE shared telemetry file, and REBAR_PLAN_REVIEW_BUDGET changes which criteria are
    # shed — both would couple the review's observable call pattern to the ambient shell.
    monkeypatch.delenv("REBAR_USAGE_LOG", raising=False)
    monkeypatch.delenv("REBAR_PLAN_REVIEW_BUDGET", raising=False)
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
    """Give each blocked fixture unique material so checkpoint reuse cannot cross tests."""
    desc = _BLOCKING_DESC + f"\nMaterial nonce (test isolation): {uuid.uuid4().hex}.\n"
    return rebar.create_ticket("task", "blocked plan", description=desc, repo_root=str(repo))


def _sidecar_count(repo: Path, tid: str) -> int:
    ticket_dir = repo / ".tickets-tracker" / tid
    return len(list(ticket_dir.glob(f"*-{sidecar.EVENT_TYPE}.json")))


# ── AC1: an unchanged BLOCK is reused with ZERO LLM calls ───────────────────────
def test_unchanged_block_reuses_verdict_with_zero_llm_calls(repo: Path) -> None:
    tid = _make_blocked(repo)
    runner = _CountingGateFake()

    v1 = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    assert v1["verdict"] == "BLOCK"
    assert v1["blocking"]  # the LLM-tier F1 block is a stored, surfaced finding
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
        # A fresh nonce keeps the EDITED material per-test-unique too (same checkpoint
        # cross-read discipline as _make_blocked), while still being a material change.
        description=_BLOCKING_DESC
        + f"\nMaterial nonce (test isolation): {uuid.uuid4().hex}.\n"
        + "\nAn edited paragraph — the remediation attempt.\n",
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
