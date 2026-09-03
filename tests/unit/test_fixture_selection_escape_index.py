"""Batched escaped-defect index for the fixture selector (ticket f0d1-b0cf-0690-4b3f).

``select_from_corpus`` used to compile full ticket state — ``show_ticket(include_inbound=True)``
— once per review-bearing ticket before emitting a single candidate. That reader derives its
inbound half by byte-scanning every event file in the store, so it cost ~5s a ticket, and
because ``escaped_defect`` is a set-UNION over every review backing a candidate the population
needing an answer is most of the review-bearing tickets (1554 of 1960 on rebar's own tracker),
not one ticket per candidate. Hours of CPU before the first row.

These tests pin the replacement: ONE store-wide ``labels.build_escaped_ticket_index`` pass.
The eager per-ticket path stays reachable through ``read_ticket_state``, so the byte-identical
check below diffs the batched default against the REAL original reader
(``labels._read_ticket_state_via_env_override``) over the same fixture corpus — not against a
snapshot generated from the new code.
"""

from __future__ import annotations

import json
import subprocess
import uuid as uuidlib
from pathlib import Path

import pytest

from rebar.llm.evals import fixture_selection
from rebar.llm.evals.fixture_selection import select_from_corpus, write_manifest
from rebar.llm.evals.plan_replay import labels
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import material_fingerprint

pytestmark = pytest.mark.unit

_CRITERION = "T2"  # a packaged plan-review criterion with committed rubric history
_REPO_ROOT = str(Path(__file__).resolve().parents[2])

# Ticket ids, and the escape route each one exercises.
_CLEAN = "0000-0000-0000-0dd0"  # no escape signal at all
_CLOSED_DEFECT = "0000-0000-0000-0aa0"  # closed with close_class=plan_defect
_CULPRIT = "0000-0000-0000-0bb0"  # named by an inbound caused_by from the bug below
_BUG = "0000-0000-0000-0cc0"  # the bug carrying the outbound caused_by (no reviews)

# Review timestamps postdate the T2 rubric's last commit so every review clears the vintage
# gate. _CLEAN reviews come FIRST so its representative uuid sorts lowest: with tier and
# margin equal across all three candidates, only the escaped-defect promotion can move the
# other two above it.
_REVIEW_TS = {
    _CLEAN: (1_900_000_300_000_000_000, 1_900_000_400_000_000_000),
    _CLOSED_DEFECT: (1_900_000_500_000_000_000, 1_900_000_600_000_000_000),
    _CULPRIT: (1_900_000_700_000_000_000, 1_900_000_800_000_000_000),
}
_NORM = {_CLEAN: "n-clean", _CLOSED_DEFECT: "n-closed", _CULPRIT: "n-culprit"}
_DESCRIPTION = "Plan text for the batched escaped-defect index regression."


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _write_event(repo: Path, ticket: str, ts: int, etype: str, data: dict) -> None:
    d = repo / ticket
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
    _git(repo, "commit", "-q", "-m", f"{etype} {ticket}")


def _fingerprint(ticket: str) -> str:
    # mirror corpus._build_context for a CREATE-only reconstruction (title="", empty state)
    return material_fingerprint(
        PlanContext(
            ticket_id=ticket,
            ticket_type="story",
            title="",
            description=_DESCRIPTION,
            state={"file_impact": []},
            children=[],
        )
    )


def _create(repo: Path, ticket: str, ts: int, ticket_type: str, alias: str) -> None:
    _write_event(
        repo,
        ticket,
        ts,
        "CREATE",
        {
            "id": ticket,
            "ticket_type": ticket_type,
            "title": f"Fixture ticket {alias}",
            "description": _DESCRIPTION,
            "parent_id": "",
            "priority": 2,
            "tags": [],
            "alias": alias,
            "creation_channel": "cli",
        },
    )


def _reviewed_story(repo: Path, ticket: str, create_ts: int) -> None:
    _create(repo, ticket, create_ts, "story", f"story-{ticket[-4:]}")
    fingerprint = _fingerprint(ticket)
    for ts in _REVIEW_TS[ticket]:
        _write_event(
            repo,
            ticket,
            ts,
            "REVIEW_RESULT",
            {
                "schema": "plan_review_result_v2",
                "ticket_id": ticket,
                "ticket_type": "story",
                "verdict": "BLOCK",
                "material_fingerprint": fingerprint,
                "reviewed_related_material": [],
                "findings": [
                    {
                        "norm_id": _NORM[ticket],
                        "criteria": [_CRITERION],
                        "cohort": [_CRITERION],
                        "decision_margin": 0.20,
                        "decision": "block",
                    }
                ],
                "provider_provenance": {"ran_model": None},
            },
        )


def _build_tracker(root: Path) -> Path:
    tracker = root / "tickets"
    tracker.mkdir(parents=True)
    _git(tracker, "init", "-q")
    _git(tracker, "config", "user.email", "t@example.com")
    _git(tracker, "config", "user.name", "T")

    _reviewed_story(tracker, _CLEAN, 1_900_000_000_000_000_001)
    _reviewed_story(tracker, _CLOSED_DEFECT, 1_900_000_000_000_000_002)
    _reviewed_story(tracker, _CULPRIT, 1_900_000_000_000_000_003)
    _create(tracker, _BUG, 1_900_000_000_000_000_004, "bug", "bug-cc00")

    # escape route 1: the reviewed story itself closed as a plan_defect
    _write_event(
        tracker,
        _CLOSED_DEFECT,
        1_900_000_900_000_000_000,
        "STATUS",
        {"current_status": "open", "status": "closed", "close_class": "plan_defect"},
    )
    # escape route 2: a bug names the reviewed story as its culprit (OUTBOUND on the bug,
    # so the culprit only ever sees it inbound)
    _write_event(
        tracker,
        _BUG,
        1_900_001_000_000_000_000,
        "LINK",
        {"target_id": _CULPRIT, "relation": "caused_by"},
    )
    return tracker


def _run(tracker: Path, cache_dir: Path, **kwargs) -> list[dict]:
    return select_from_corpus(
        repo_root=_REPO_ROOT,
        tracker_path=str(tracker),
        base_ref="HEAD",
        cache_dir=cache_dir,
        criteria_ids=[_CRITERION],
        **kwargs,
    )


def _by_norm(rows: list[dict]) -> dict[str, dict]:
    return {r["norm_id"]: r for r in rows if r["kind"] == "candidate"}


# ── AC: the fixture corpus really exercises both escape routes ───────────────────────


def test_index_finds_both_escape_routes_and_nothing_else(tmp_path):
    tracker = _build_tracker(tmp_path)
    assert labels.build_escaped_ticket_index(str(tracker)) == frozenset({_CLOSED_DEFECT, _CULPRIT})


def test_index_agrees_with_the_per_ticket_reader_on_every_ticket(tmp_path):
    """The batched index must be the same predicate as ``escaped_defect`` over the real
    per-ticket reader, ticket for ticket — that equivalence is what makes the swap safe."""
    tracker = _build_tracker(tmp_path)
    index = labels.build_escaped_ticket_index(str(tracker))
    for ticket in (_CLEAN, _CLOSED_DEFECT, _CULPRIT, _BUG):
        eager = labels.escaped_defect(
            labels._read_ticket_state_via_env_override(ticket, str(tracker))
        )
        assert (ticket in index) is eager, ticket


# ── AC: emitted rows are byte-identical to the eager implementation ──────────────────


def test_manifest_is_byte_identical_to_the_eager_per_ticket_path(tmp_path):
    """Runs BOTH paths over one fixture corpus and compares the written JSONL byte for byte.

    The "old" side is the real pre-fix reader, ``labels._read_ticket_state_via_env_override``
    — the exact callable ``select_from_corpus`` used to default to — routed through the
    ``read_ticket_state`` parameter, so this is a live diff of two implementations rather
    than a comparison against a stored snapshot.
    """
    tracker = _build_tracker(tmp_path)

    eager_rows = _run(
        tracker,
        tmp_path / "c-eager",
        read_ticket_state=labels._read_ticket_state_via_env_override,
    )
    batched_rows = _run(tracker, tmp_path / "c-batched")

    eager_path = tmp_path / "eager.jsonl"
    batched_path = tmp_path / "batched.jsonl"
    write_manifest(eager_rows, eager_path)
    write_manifest(batched_rows, batched_path)

    assert batched_path.read_bytes() == eager_path.read_bytes()
    # and the corpus is not trivially empty: three ranked candidates flowed through
    assert len(_by_norm(batched_rows)) == 3


# ── AC: no per-review-bearing-ticket show_ticket fan-out ─────────────────────────────


def test_default_path_never_compiles_per_ticket_state(tmp_path, monkeypatch):
    """The reader is not called for a REDUCED set — it is not called at all.

    Asserted by making the per-ticket compile explode: if the default path still reached for
    it (for any ticket in the union of tickets backing an emitted candidate, which is what
    ``escaped_defect`` unions over), this raises instead of returning rows.
    """
    tracker = _build_tracker(tmp_path)
    expected = _run(tracker, tmp_path / "c-ref")

    calls: list[str] = []

    def _explode(ticket_id: str, tracker_path: str) -> dict:
        calls.append(ticket_id)
        raise AssertionError(f"per-ticket state compiled for {ticket_id}")

    monkeypatch.setattr(labels, "_read_ticket_state_via_env_override", _explode)
    monkeypatch.setattr("rebar.show_ticket", _explode)

    rows = _run(tracker, tmp_path / "c-noreader")
    assert calls == []
    assert rows == expected


def test_index_is_built_once_per_selection_run(tmp_path, monkeypatch):
    tracker = _build_tracker(tmp_path)
    builds: list[str] = []
    real = labels.build_escaped_ticket_index

    def _counting(tracker_path: str):
        builds.append(tracker_path)
        return real(tracker_path)

    monkeypatch.setattr(labels, "build_escaped_ticket_index", _counting)
    _run(tracker, tmp_path / "c")
    assert len(builds) == 1


# ── AC: escaped_defect fidelity — both routes still rank-promote ─────────────────────


def test_both_escape_routes_still_rank_promote(tmp_path):
    """Held-out: the two escaped candidates outrank the clean one despite the clean one
    owning the lowest representative uuid (the final tiebreak) — so only the escaped-defect
    key, the 2nd sort key, can explain the order. Tier and abs_margin are equal across all
    three."""
    tracker = _build_tracker(tmp_path)
    rows = _run(tracker, tmp_path / "c")
    by_norm = _by_norm(rows)

    assert by_norm[_NORM[_CLOSED_DEFECT]]["escaped_defect"] is True
    assert by_norm[_NORM[_CULPRIT]]["escaped_defect"] is True
    assert by_norm[_NORM[_CLEAN]]["escaped_defect"] is False

    assert {r["tier"] for r in by_norm.values()} == {"advisory"}
    assert all(r["abs_margin"] == pytest.approx(0.20) for r in by_norm.values())

    clean_rank = by_norm[_NORM[_CLEAN]]["rank"]
    assert by_norm[_NORM[_CLOSED_DEFECT]]["rank"] < clean_rank
    assert by_norm[_NORM[_CULPRIT]]["rank"] < clean_rank
    # the clean candidate would otherwise have won the uuid tiebreak
    assert by_norm[_NORM[_CLEAN]]["review_event_uuid"] < min(
        by_norm[_NORM[_CLOSED_DEFECT]]["review_event_uuid"],
        by_norm[_NORM[_CULPRIT]]["review_event_uuid"],
    )


def test_precomputed_escaped_defect_wins_over_ticket_state(tmp_path):
    """``_escaped`` honours a precomputed answer; without one it still reads ticket_state,
    which is what keeps the eager path working unchanged."""
    assert fixture_selection._escaped({"escaped_defect": True, "ticket_state": {}}) is True
    assert (
        fixture_selection._escaped(
            {"escaped_defect": False, "ticket_state": {"close_class": "plan_defect"}}
        )
        is False
    )
    assert fixture_selection._escaped({"ticket_state": {"close_class": "plan_defect"}}) is True
    assert fixture_selection._escaped({"ticket_state": {}}) is False
