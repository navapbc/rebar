"""Held-out determinism + enrichment E2E for the fixture selector (ticket 549b, AC10).

THE IMPLEMENTER MUST NOT SEE THIS FILE. Two consecutive real runs of
:func:`select_from_corpus` over the same committed tickets-tracker git history must emit a
byte-identical JSONL manifest — proving the whole pipeline (git-object walk, VERIFIED-row
finding enrichment, vintage gate, selection, JSONL write) is deterministic and offline. The
reviews carry a fingerprint that matches the reconstructed material so ``build_corpus`` marks
them verified and their findings actually flow through — the two equal-material reviews then
produce a real reproduction-consensus candidate, not merely a zero row. Runs under
tests/unit/** where the repository's network-escape guard is active.
"""

from __future__ import annotations

import json
import subprocess
import uuid as uuidlib
from pathlib import Path

import pytest

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.fixture_selection import select_from_corpus, write_manifest
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import material_fingerprint

pytestmark = pytest.mark.unit

_CRITERION = "T2"  # a packaged plan-review criterion with committed rubric history
_TICKET = "0000-0000-0000-0abc"
_DESCRIPTION = "Plan text for the fixture-selection determinism E2E."
# postdates the T2 rubric's last commit (~1.78e18 ns) so the reviews clear the vintage gate
_REVIEW_TS = (1_900_000_100_000_000_000, 1_900_000_200_000_000_000)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _write_event(repo: Path, ts: int, etype: str, data: dict) -> None:
    d = repo / _TICKET
    d.mkdir(parents=True, exist_ok=True)
    ev_uuid = str(uuidlib.UUID(int=ts % (2**128)))
    envelope = {
        "author": "T",
        "author_email": "t@example.com",
        "data": data,
        "env_id": "00000000-0000-0000-0000-000000000000",
        "event_type": etype,
        "timestamp": ts,
        "uuid": ev_uuid,
    }
    (d / f"{ts}-{ev_uuid}-{etype}.json").write_text(json.dumps(envelope))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"{etype} {_TICKET}")


def _verified_fingerprint() -> str:
    # mirror corpus._build_context for a CREATE-only reconstruction (title="", empty state)
    ctx = PlanContext(
        ticket_id=_TICKET,
        ticket_type="story",
        title="",
        description=_DESCRIPTION,
        state={"file_impact": []},
        children=[],
    )
    return material_fingerprint(ctx)


def _finding() -> dict:
    return {
        "norm_id": "n1",
        "criteria": [_CRITERION],
        "cohort": [_CRITERION],
        "decision_margin": 0.20,
        "decision": "block",
    }


def _build_tracker(root: Path) -> Path:
    tracker = root / "tickets"
    tracker.mkdir(parents=True)
    _git(tracker, "init", "-q")
    _git(tracker, "config", "user.email", "t@example.com")
    _git(tracker, "config", "user.name", "T")
    _write_event(
        tracker,
        1_900_000_000_000_000_001,
        "CREATE",
        {
            "id": _TICKET,
            "ticket_type": "story",
            "title": "Fixture-selection E2E story",
            "description": _DESCRIPTION,
            "parent_id": "",
            "priority": 2,
            "tags": [],
            "alias": "fixture-selection-e2e-story",
            "creation_channel": "cli",
        },
    )
    fp = _verified_fingerprint()
    for ts in _REVIEW_TS:
        _write_event(
            tracker,
            ts,
            "REVIEW_RESULT",
            {
                "schema": "plan_review_result_v2",
                "ticket_id": _TICKET,
                "ticket_type": "story",
                "verdict": "BLOCK",
                "material_fingerprint": fp,
                "reviewed_related_material": [],
                "findings": [_finding()],
                "provider_provenance": {"ran_model": None},
            },
        )
    return tracker


def _run(repo_root: str, tracker: Path, cache_dir: Path) -> list[dict]:
    return select_from_corpus(
        repo_root=repo_root,
        tracker_path=str(tracker),
        base_ref="HEAD",
        cache_dir=cache_dir,
        criteria_ids=[_CRITERION],
    )


def test_two_runs_emit_byte_identical_manifest_with_real_candidate(tmp_path):
    assert criterion_prompt_id(_CRITERION, gate_key="plan_review")  # packaged prompt id

    repo_root = str(Path(__file__).resolve().parents[2])  # the worktree root (real rubrics)
    tracker = _build_tracker(tmp_path)

    rows1 = _run(repo_root, tracker, tmp_path / "c1")
    rows2 = _run(repo_root, tracker, tmp_path / "c2")

    out1 = tmp_path / "m1.jsonl"
    out2 = tmp_path / "m2.jsonl"
    write_manifest(rows1, out1)
    write_manifest(rows2, out2)

    # AC10: byte-identical across runs
    assert out1.read_bytes() == out2.read_bytes()
    # every emitted line is sorted-key JSON
    for line in out1.read_text(encoding="utf-8").splitlines():
        assert json.dumps(json.loads(line), sort_keys=True) == line
    # enrichment actually flowed: the two verified equal-fingerprint reviews both fire T2, so
    # the candidate carries BOTH reproduction_consensus (re-reviews of unchanged material) and
    # margin (decision_margin 0.20) — not merely a zero row
    cands = [r for r in rows1 if r["kind"] == "candidate"]
    assert len(cands) == 1
    assert cands[0]["criterion"] == _CRITERION
    assert cands[0]["direction"] == "fire"
    assert cands[0]["norm_id"] == "n1"
    assert set(cands[0]["signals"]) == {"margin", "reproduction_consensus"}
    assert cands[0]["abs_margin"] == pytest.approx(0.20)


def test_corpus_enrichment_loads_real_ticket_state_for_escaped_defect(tmp_path):
    """Regression (change 2552 LLM-Review): select_from_corpus must enrich each review with
    its ticket's compiled state so the escaped-defect priority signal can fire. Before the
    fix the enrichment loop hardwired ``ticket_state = {}``, so ``escaped_defect`` was dead in
    the real path regardless of the ticket. Injecting a reader that reports the reviewed
    ticket closed as a ``plan_defect`` must surface ``escaped_defect: True`` on its candidate.
    """
    repo_root = str(Path(__file__).resolve().parents[2])
    tracker = _build_tracker(tmp_path)

    def _reader(ticket_id: str, tracker_path: str) -> dict:
        # the reviewed ticket is itself an escaped plan defect; any other ticket is clean
        if ticket_id == _TICKET:
            return {"close_class": "plan_defect", "status": "closed"}
        return {}

    rows = select_from_corpus(
        repo_root=repo_root,
        tracker_path=str(tracker),
        base_ref="HEAD",
        cache_dir=tmp_path / "c",
        criteria_ids=[_CRITERION],
        read_ticket_state=_reader,
    )
    cands = [r for r in rows if r["kind"] == "candidate"]
    assert len(cands) == 1
    assert cands[0]["criterion"] == _CRITERION
    assert cands[0]["escaped_defect"] is True
