"""BLOCK verdict reuse (bug 7e77 — masterful-whimsical-tigermoth).

``_idempotent_reuse`` (feature b3e5) covers only PASS: it requires a certified
attestation, and a BLOCK never signs. So re-running ``review-plan`` on an UNCHANGED
blocked plan re-paid the full multi-pass LLM review every time. The fix adds a
``verdict_reuse`` path beside it: when no attestation applies, the stored BLOCK
verdict in the latest ``REVIEW_RESULT`` sidecar is reused — zero LLM calls — when
its ``material_fingerprint`` AND ``verified_at_sha`` both still match the current
plan/code, and ``--force`` bypasses it. A reused BLOCK exits 1, renders the stored
findings with a "reused" marker, and emits NO new sidecar.

Offline end-to-end: an LLM-tier BLOCK (the counting fake emits an F1 finding —
blocking posture, threshold 0.60 — verified full-yes at high severity) with a
schema-aware counting fake runner, so no model/network. The fixture plan PASSES
the deterministic floor on purpose: story 228b short-circuits any DET-blocked
plan BEFORE the LLM tier with zero finder calls, so a DET-floor BLOCK (the old
fixture) can no longer serve as this file's oracle that the finder re-ran under
``--force`` / after code drift.
"""

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
    """Shape-valid offline runner for the plan-review passes (mirrors the lifecycle gate
    tests' fake) that COUNTS invocations — the oracle for "reuse ran zero LLM calls".

    Unlike the lifecycle fake it MANUFACTURES a BLOCK from the LLM tier: the finder
    chunk that carries ``_BLOCK_CRITERION`` returns one finding tagged with it, and the
    verifier answers every graded binary "yes" at high severity, so Pass-3 decides
    "block" (priority 1.0 ≥ the 0.60 threshold). The plan itself passes the DET floor
    — a DET block would short-circuit with zero finder calls (story 228b) and defeat
    the "the finder re-ran" oracles below."""

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
    """Create the LLM-blocked fixture ticket with PER-TEST-UNIQUE plan material.

    Pass-1 chunk checkpoints (`.rebar/cache/plan-review/<ticket_id>/`) are keyed by the
    ticket's MATERIAL fingerprint; today that fingerprint includes the ticket id and the
    cache dir lives under the per-test repo, but two tests sharing byte-identical fixture
    material would be one fingerprint-derivation change away from reading each other's
    cached chunks (and a cached chunk makes ZERO finder calls, silently breaking this
    file's call-count oracles). A per-ticket nonce paragraph makes the material unique by
    construction, so no cache — present or future — can serve one test's chunks to
    another regardless of xdist worker placement."""
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
