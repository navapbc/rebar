"""The ``latest_review_result`` REVIEW_RESULT sidecar reader (child e344).

A remediation re-review hands the Pass-2 novelty sub-call its OWN prior findings; the
reader is the seam that fetches them. These tests pin its contract end-to-end over a
real git-backed tracker: it returns the most-recent sidecar payload (with the e344 prose
fields), degrades to ``None`` on the empty/absent/malformed/foreign-schema cases (never
raises), and the retention prune still bounds growth after the prose fields were added.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import rebar
from rebar import config as _config
from rebar.llm.plan_review import sidecar


def _verdict(ticket_id: str, finding_text: str) -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": ticket_id,
        "ticket_type": "task",
        "advisory": [
            {
                "id": "f1",
                "finding": finding_text,
                "suggested_fix": "Do the thing.",
                "checklist_item": "- [ ] Do the thing.",
                "criteria": ["T5a"],
                "location": "Scope",
                "tier": "LLM",
                "decision": "advisory",
                "priority": 0.4,
            }
        ],
        "coverage": {"metrics": {}},
        "coaching": [],
    }


def _make_ticket(repo: Path) -> str:
    return rebar.create_ticket(
        "task",
        "reader fixture ticket",
        description="x" * 50,
        repo_root=str(repo),
    )


def test_latest_review_result_returns_newest_payload_with_prose(rebar_repo: Path) -> None:
    """Two reviews emitted in order → the reader returns the SECOND (newest) payload,
    and that payload carries the e344 prose fields the novelty sub-call re-grounds on."""
    tid = _make_ticket(rebar_repo)
    assert sidecar.emit(
        _verdict(tid, "first review finding"), material="m1", repo_root=str(rebar_repo)
    )
    assert sidecar.emit(
        _verdict(tid, "second review finding"), material="m2", repo_root=str(rebar_repo)
    )

    got = sidecar.latest_review_result(tid, repo_root=str(rebar_repo))
    assert got is not None
    # A fresh emit is now the lossless v2 record (story 4e19); the reader accepts both.
    assert got["schema"] == "plan_review_result_v2"
    assert got["material_fingerprint"] == "m2"  # newest
    assert got["findings"][0]["finding"] == "second review finding"
    assert got["findings"][0]["suggested_fix"] == "Do the thing."
    assert got["findings"][0]["checklist_item"] == "- [ ] Do the thing."


def test_latest_review_result_none_when_no_prior_review(rebar_repo: Path) -> None:
    """The common first-review case: a ticket with no sidecar yet → None (the caller
    proceeds with no prior findings), and a never-created ticket id is also None."""
    tid = _make_ticket(rebar_repo)
    assert sidecar.latest_review_result(tid, repo_root=str(rebar_repo)) is None
    assert sidecar.latest_review_result("ffff-ffff-ffff-ffff", repo_root=str(rebar_repo)) is None


def test_latest_review_result_none_on_malformed_json(rebar_repo: Path) -> None:
    """A partially-written/garbled newest sidecar → None (never raises)."""
    tid = _make_ticket(rebar_repo)
    assert sidecar.emit(_verdict(tid, "ok finding"), material="m1", repo_root=str(rebar_repo))
    tracker = str(_config.tracker_dir(str(rebar_repo)))
    from rebar._engine_support.resolver import resolve_ticket_id

    rid = resolve_ticket_id(tid, tracker) or tid
    ticket_dir = os.path.join(tracker, rid)
    files = sorted(f for f in os.listdir(ticket_dir) if f.endswith("-REVIEW_RESULT.json"))
    with open(os.path.join(ticket_dir, files[-1]), "w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json")
    assert sidecar.latest_review_result(tid, repo_root=str(rebar_repo)) is None


def test_latest_review_result_walks_back_past_corrupt_newest(rebar_repo: Path) -> None:
    """A corrupt NEWEST sidecar (e.g. a mid-emit crash) must not blind the caller to an
    older valid review: the reader walks back and returns the older valid v1 payload."""
    tid = _make_ticket(rebar_repo)
    assert sidecar.emit(
        _verdict(tid, "older valid finding"), material="m1", repo_root=str(rebar_repo)
    )
    assert sidecar.emit(_verdict(tid, "newer finding"), material="m2", repo_root=str(rebar_repo))
    tracker = str(_config.tracker_dir(str(rebar_repo)))
    from rebar._engine_support.resolver import resolve_ticket_id

    rid = resolve_ticket_id(tid, tracker) or tid
    ticket_dir = os.path.join(tracker, rid)
    files = sorted(f for f in os.listdir(ticket_dir) if f.endswith("-REVIEW_RESULT.json"))
    with open(os.path.join(ticket_dir, files[-1]), "w", encoding="utf-8") as fh:
        fh.write("{ corrupt mid-emit")  # clobber the NEWEST

    got = sidecar.latest_review_result(tid, repo_root=str(rebar_repo))
    assert got is not None
    assert got["material_fingerprint"] == "m1"  # recovered the older valid review
    assert got["findings"][0]["finding"] == "older valid finding"


def test_latest_review_result_schema_guard_rejects_foreign_payload(rebar_repo: Path) -> None:
    """A newest sidecar whose schema is neither plan_review_result_v1 nor _v2 is rejected →
    None, so a FUTURE schema bump can never feed a stale shape to the novelty sub-call."""
    tid = _make_ticket(rebar_repo)
    assert sidecar.emit(_verdict(tid, "ok finding"), material="m1", repo_root=str(rebar_repo))
    tracker = str(_config.tracker_dir(str(rebar_repo)))
    from rebar._engine_support.resolver import resolve_ticket_id

    rid = resolve_ticket_id(tid, tracker) or tid
    ticket_dir = os.path.join(tracker, rid)
    files = sorted(f for f in os.listdir(ticket_dir) if f.endswith("-REVIEW_RESULT.json"))
    path = os.path.join(ticket_dir, files[-1])
    event = json.load(open(path, encoding="utf-8"))
    event["data"]["schema"] = "plan_review_result_v2_future"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(event, fh)
    assert sidecar.latest_review_result(tid, repo_root=str(rebar_repo)) is None


def test_prune_bound_still_respected_after_prose_fields(rebar_repo: Path) -> None:
    """Adding the prose fields to _slim does not bypass the RETAIN_PER_TICKET prune
    bound: after emitting more than the bound, only the most-recent RETAIN remain, and
    the reader still returns the newest."""
    tid = _make_ticket(rebar_repo)
    n = sidecar.RETAIN_PER_TICKET + 3
    for i in range(n):
        assert sidecar.emit(
            _verdict(tid, f"finding {i}"), material=f"m{i}", repo_root=str(rebar_repo)
        )

    tracker = str(_config.tracker_dir(str(rebar_repo)))
    from rebar._engine_support.resolver import resolve_ticket_id

    rid = resolve_ticket_id(tid, tracker) or tid
    ticket_dir = os.path.join(tracker, rid)
    remaining = [f for f in os.listdir(ticket_dir) if f.endswith("-REVIEW_RESULT.json")]
    assert len(remaining) == sidecar.RETAIN_PER_TICKET

    got = sidecar.latest_review_result(tid, repo_root=str(rebar_repo))
    assert got is not None
    assert got["material_fingerprint"] == f"m{n - 1}"  # newest survives the prune


def test_reader_accepts_both_v1_and_v2_schemas(rebar_repo: Path) -> None:
    """Story 4e19: the reader accepts BOTH plan_review_result_v1 and _v2. A hand-written v1
    record (predating the new fields) reads back cleanly and WITHOUT evidence/scenarios/
    threshold; a fresh v2 emit reads back WITH them, newest-first."""
    from rebar._commands._seam import append_event

    tid = _make_ticket(rebar_repo)
    tracker = _config.tracker_dir(str(rebar_repo))
    # A genuine v1 record: no evidence/scenarios/block_threshold/blocking_enabled on the finding.
    v1_payload = {
        "schema": "plan_review_result_v1",
        "verdict": "PASS",
        "ticket_id": tid,
        "material_fingerprint": "m-v1",
        "findings": [
            {"id": "old", "finding": "legacy finding", "criteria": ["C1"], "location": "L"}
        ],
        "coaching": [],
    }
    append_event(tid, "REVIEW_RESULT", v1_payload, tracker, repo_root=str(rebar_repo))

    got_v1 = sidecar.latest_review_result(tid, repo_root=str(rebar_repo))
    assert got_v1 is not None
    assert got_v1["schema"] == "plan_review_result_v1"
    assert got_v1["findings"][0]["finding"] == "legacy finding"
    # the reader does not choke on the absence of the new fields on a v1 record
    assert "evidence" not in got_v1["findings"][0]
    assert "block_threshold" not in got_v1["findings"][0]

    # A fresh v2 emit on the same ticket: carries the new fields, and is returned as newest.
    v = _verdict(tid, "modern finding")
    v["advisory"][0]["evidence"] = ["grounding quote"]
    v["advisory"][0]["scenarios"] = ["the boundary case"]
    v["advisory"][0]["block_threshold"] = 0.7
    v["advisory"][0]["blocking_enabled"] = True
    assert sidecar.emit(v, material="m-v2", repo_root=str(rebar_repo))

    got_v2 = sidecar.latest_review_result(tid, repo_root=str(rebar_repo))
    assert got_v2 is not None
    assert got_v2["schema"] == "plan_review_result_v2"  # newest
    assert got_v2["findings"][0]["evidence"] == ["grounding quote"]
    assert got_v2["findings"][0]["scenarios"] == ["the boundary case"]
    assert got_v2["findings"][0]["block_threshold"] == 0.7
    assert got_v2["findings"][0]["blocking_enabled"] is True
