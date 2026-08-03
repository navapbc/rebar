"""Epic-close bug screen (ticket 4b54): DET caused_by floor, candidate filter, haiku
screen orchestration, forwarding cap, sidecar tally.

Agents file bugs OUTSIDE an epic's hierarchy during epic execution and deem them
out-of-scope even when they are defects in the epic's own deliverable; the direct-children
close gate cannot see them. The gate here is three-staged: a deterministic ``caused_by``
floor (hard block), a deterministic candidate filter (status/type + created-after-claim OR
linked-to-subtree), and an LLM relevance screen whose A-verdicts are forwarded compactly to
the completion verifier for store-grounded disposition adjudication.

Every test here is LLM-free at BOTH tiers: the screen runs through a FAKE forced-choice
verdict map (the ``screen_fn`` seam), and the verifier path is asserted UP TO the forwarded
candidate block inside the precheck-assembled fenced context. The REAL haiku screen and the
REAL verifier's disposition rule (must-block 30a2/5b09; must-pass 30d3/e6a0/c8ed) are proven
exclusively in the ticket's [operator-attested] live calibration AC.

Regression encodings from the event-precise backtest over 56 epic closes:

* 30a2-shape (must-block tier): a bug created during the epic window, unlinked, screened A
  → forwarded to the verifier (``test_regression_30a2_shape_forwarded``).
* c8ed-shape (must-pass tier): a bug that supersedes the epic — linked, so a candidate; the
  verifier (not the filter) adjudicates via its supersedes link
  (``test_regression_c8ed_shape_is_candidate``).
* 22f5-shape (not-flagged): fixed during the epic, CLOSED at close time → excluded by the
  status filter (``test_closed_bugs_excluded``).
* 5e94-shape (not-flagged): created 13 days POST-close — does not exist when the gate runs,
  so it is trivially absent from any candidate enumeration (no test can time-travel; the
  status/type filter is the enforced surface).
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

import rebar
from rebar.llm import completion
from rebar.llm import completion_sidecar as sidecar
from rebar.llm import epic_bug_screen as ebs

pytestmark = pytest.mark.unit


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _epic_with_child(repo: Path) -> tuple[str, str]:
    """An epic with one child task, epic claimed (first in_progress event exists)."""
    epic = rebar.create_ticket("epic", "widget pipeline rework", repo_root=str(repo))
    child = rebar.create_ticket("task", "swap widget parser", parent=epic, repo_root=str(repo))
    rebar.claim(epic, repo_root=str(repo))
    return epic, child


# ------------------------------------------------------------------ DET caused_by floor


def test_open_caused_by_bug_blocks_with_teaching_message(rebar_repo: Path) -> None:
    epic, child = _epic_with_child(rebar_repo)
    bug = rebar.create_ticket("bug", "parser drops widgets", repo_root=str(rebar_repo))
    rebar.link(bug, child, "caused_by", repo_root=str(rebar_repo))

    found = completion.epic_bug_floor_findings(epic, str(rebar_repo))

    assert len(found) == 1
    detail = found[0]["detail"]
    assert bug in detail
    # The teaching message: fix it, re-parent it, or dispute the link.
    for verb in ("fix", "re-parent", "dispute"):
        assert verb in detail.lower()


def test_caused_by_targeting_the_epic_itself_blocks(rebar_repo: Path) -> None:
    epic, _child = _epic_with_child(rebar_repo)
    bug = rebar.create_ticket("bug", "epic broke the build", repo_root=str(rebar_repo))
    rebar.link(bug, epic, "caused_by", repo_root=str(rebar_repo))

    assert len(completion.epic_bug_floor_findings(epic, str(rebar_repo))) == 1


def test_in_progress_caused_by_bug_blocks(rebar_repo: Path) -> None:
    epic, child = _epic_with_child(rebar_repo)
    bug = rebar.create_ticket("bug", "parser drops widgets", repo_root=str(rebar_repo))
    rebar.link(bug, child, "caused_by", repo_root=str(rebar_repo))
    rebar.claim(bug, repo_root=str(rebar_repo))

    assert len(completion.epic_bug_floor_findings(epic, str(rebar_repo))) == 1


def test_closed_caused_by_bug_passes(rebar_repo: Path) -> None:
    epic, child = _epic_with_child(rebar_repo)
    bug = rebar.create_ticket("bug", "parser drops widgets", repo_root=str(rebar_repo))
    rebar.link(bug, child, "caused_by", repo_root=str(rebar_repo))
    rebar.transition(bug, "open", "closed", close_class="regression", repo_root=str(rebar_repo))

    assert completion.epic_bug_floor_findings(epic, str(rebar_repo)) == []


def test_discovered_from_alone_does_not_trip_the_floor(rebar_repo: Path) -> None:
    epic, child = _epic_with_child(rebar_repo)
    bug = rebar.create_ticket("bug", "parser drops widgets", repo_root=str(rebar_repo))
    rebar.link(bug, child, "discovered_from", repo_root=str(rebar_repo))

    assert completion.epic_bug_floor_findings(epic, str(rebar_repo)) == []


def test_caused_by_outside_the_subtree_does_not_block(rebar_repo: Path) -> None:
    epic, _child = _epic_with_child(rebar_repo)
    stranger = rebar.create_ticket("task", "unrelated refactor", repo_root=str(rebar_repo))
    bug = rebar.create_ticket("bug", "stranger broke things", repo_root=str(rebar_repo))
    rebar.link(bug, stranger, "caused_by", repo_root=str(rebar_repo))

    assert completion.epic_bug_floor_findings(epic, str(rebar_repo)) == []


# ------------------------------------------------------------------ DET candidate filter


def _candidate_ids(epic: str, repo: Path) -> list[str]:
    cands, _overflow = completion.epic_bug_candidates(epic, str(repo))
    return [c["ticket_id"] for c in cands]


def test_bug_created_after_first_in_progress_is_a_candidate(rebar_repo: Path) -> None:
    epic, _child = _epic_with_child(rebar_repo)  # claim happens inside
    bug = rebar.create_ticket("bug", "fresh unlinked bug", repo_root=str(rebar_repo))

    assert bug in _candidate_ids(epic, rebar_repo)


def test_bug_created_before_claim_and_unlinked_is_excluded(rebar_repo: Path) -> None:
    epic = rebar.create_ticket("epic", "widget pipeline rework", repo_root=str(rebar_repo))
    rebar.create_ticket("task", "swap widget parser", parent=epic, repo_root=str(rebar_repo))
    old_bug = rebar.create_ticket("bug", "ancient unrelated bug", repo_root=str(rebar_repo))
    rebar.claim(epic, repo_root=str(rebar_repo))  # anchor is AFTER the bug's creation

    assert old_bug not in _candidate_ids(epic, rebar_repo)


def test_linked_bug_is_a_candidate_regardless_of_creation_time(rebar_repo: Path) -> None:
    epic = rebar.create_ticket("epic", "widget pipeline rework", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "swap parser", parent=epic, repo_root=str(rebar_repo))
    old_bug = rebar.create_ticket("bug", "old but linked", repo_root=str(rebar_repo))
    rebar.link(old_bug, child, "relates_to", repo_root=str(rebar_repo))
    rebar.claim(epic, repo_root=str(rebar_repo))

    assert old_bug in _candidate_ids(epic, rebar_repo)


def test_incoming_link_from_subtree_counts(rebar_repo: Path) -> None:
    """The link may live on the SUBTREE side (child -> bug); both directions qualify."""
    epic = rebar.create_ticket("epic", "widget pipeline rework", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "swap parser", parent=epic, repo_root=str(rebar_repo))
    old_bug = rebar.create_ticket("bug", "old, linked from child", repo_root=str(rebar_repo))
    rebar.link(child, old_bug, "relates_to", repo_root=str(rebar_repo))
    rebar.claim(epic, repo_root=str(rebar_repo))

    assert old_bug in _candidate_ids(epic, rebar_repo)


def test_link_to_grandchild_counts_any_depth(rebar_repo: Path) -> None:
    epic = rebar.create_ticket("epic", "widget pipeline rework", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "swap parser", parent=epic, repo_root=str(rebar_repo))
    grand = rebar.create_ticket("task", "parser subtask", parent=child, repo_root=str(rebar_repo))
    old_bug = rebar.create_ticket("bug", "old, linked to grandchild", repo_root=str(rebar_repo))
    rebar.link(old_bug, grand, "relates_to", repo_root=str(rebar_repo))
    rebar.claim(epic, repo_root=str(rebar_repo))

    assert old_bug in _candidate_ids(epic, rebar_repo)


def test_subtree_member_bugs_are_not_candidates(rebar_repo: Path) -> None:
    """A bug that IS a child of the epic belongs to the direct-children gate, not here."""
    epic, _child = _epic_with_child(rebar_repo)
    child_bug = rebar.create_ticket(
        "bug", "in-hierarchy bug", parent=epic, repo_root=str(rebar_repo)
    )

    assert child_bug not in _candidate_ids(epic, rebar_repo)


def test_closed_bugs_excluded(rebar_repo: Path) -> None:
    """22f5-shape: a bug fixed (closed) during the epic must not resurface at close."""
    epic, child = _epic_with_child(rebar_repo)
    bug = rebar.create_ticket("bug", "fixed during epic", repo_root=str(rebar_repo))
    rebar.link(bug, child, "relates_to", repo_root=str(rebar_repo))
    rebar.transition(bug, "open", "closed", close_class="regression", repo_root=str(rebar_repo))

    assert bug not in _candidate_ids(epic, rebar_repo)


def test_non_bug_types_excluded(rebar_repo: Path) -> None:
    epic, _child = _epic_with_child(rebar_repo)
    task = rebar.create_ticket("task", "fresh follow-up task", repo_root=str(rebar_repo))

    assert task not in _candidate_ids(epic, rebar_repo)


def test_anchor_falls_back_to_epic_creation_when_never_claimed(rebar_repo: Path) -> None:
    """No in_progress STATUS event at all -> anchor on the epic's created_at (wider window,
    safe direction: over-inclusion only feeds the cheap screen)."""
    epic = rebar.create_ticket("epic", "never-claimed epic", repo_root=str(rebar_repo))
    bug = rebar.create_ticket("bug", "bug after epic creation", repo_root=str(rebar_repo))

    assert bug in _candidate_ids(epic, rebar_repo)


def test_ceiling_linked_first_then_created_desc_with_overflow(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enforced screen ceiling truncates linked-first, remainder becomes overflow.

    The real ceiling is 32; patched to 5 here so the test doesn't create 35 tickets."""
    monkeypatch.setattr(completion, "EPIC_BUG_SCREEN_CEILING", 5)
    epic, child = _epic_with_child(rebar_repo)
    unlinked = [
        rebar.create_ticket("bug", f"window bug {i}", repo_root=str(rebar_repo)) for i in range(6)
    ]
    linked = rebar.create_ticket("bug", "linked bug", repo_root=str(rebar_repo))
    rebar.link(linked, child, "relates_to", repo_root=str(rebar_repo))

    cands, overflow = completion.epic_bug_candidates(epic, str(rebar_repo))

    assert len(cands) == 5
    assert overflow == 2  # 7 qualifying - 5 kept
    assert cands[0]["ticket_id"] == linked  # linked-to-subtree candidates first
    kept_unlinked = [c["ticket_id"] for c in cands[1:]]
    # then by created timestamp DESCENDING (newest window bugs first)
    assert kept_unlinked == list(reversed(unlinked))[: len(kept_unlinked)]


# ------------------------------------------------------------------ screen orchestration


def _mk_bug(i: int, verdict_map: dict) -> dict:
    bid = f"bug{i:02d}-0000-0000-0000"
    verdict_map[bid] = {"verdict": "A", "citation": f"defect in widget {i}"}
    return {"ticket_id": bid, "title": f"widget bug {i}", "description": "d", "status": "open"}


def test_screen_fake_map_tallies_every_candidate(rebar_repo: Path) -> None:
    vmap: dict = {}
    bugs = [_mk_bug(i, vmap) for i in range(3)]
    vmap[bugs[1]["ticket_id"]] = {"verdict": "C", "citation": "unrelated subsystem"}

    tally = ebs.screen_candidates(
        {"title": "epic", "description": ""},
        bugs,
        None,
        None,
        screen_fn=lambda bug, sp: vmap[bug["ticket_id"]],
    )

    assert [row["verdict"] for row in tally] == ["A", "C", "A"]
    assert all(row["ticket_id"] == b["ticket_id"] for row, b in zip(tally, bugs, strict=True))
    assert tally[0]["citation"] == "defect in widget 0"


def test_screen_warm_call_completes_before_fanout_starts(rebar_repo: Path) -> None:
    vmap: dict = {}
    bugs = [_mk_bug(i, vmap) for i in range(4)]
    events: list[tuple[str, str]] = []
    lock = threading.Lock()

    def fake(bug: dict, sp: str) -> dict:
        with lock:
            events.append(("start", bug["ticket_id"]))
        time.sleep(0.02)
        with lock:
            events.append(("end", bug["ticket_id"]))
        return vmap[bug["ticket_id"]]

    ebs.screen_candidates({"title": "e", "description": ""}, bugs, None, None, screen_fn=fake)

    warm_id = bugs[0]["ticket_id"]
    assert events[0] == ("start", warm_id)
    # The warm call must END before ANY fan-out call starts (cache-warm ordering).
    assert events[1] == ("end", warm_id)


def test_screen_per_candidate_failure_degrades_to_c(rebar_repo: Path) -> None:
    vmap: dict = {}
    bugs = [_mk_bug(i, vmap) for i in range(2)]

    def flaky(bug: dict, sp: str) -> dict:
        if bug["ticket_id"] == bugs[0]["ticket_id"]:
            raise RuntimeError("model exploded")
        return vmap[bug["ticket_id"]]

    tally = ebs.screen_candidates(
        {"title": "e", "description": ""}, bugs, None, None, screen_fn=flaky
    )

    # Warm-call failure degrades that candidate to C (non-surfacing) and the rest proceed.
    assert [row["verdict"] for row in tally] == ["C", "A"]


def test_malformed_verdict_normalizes_to_c() -> None:
    model = __import__(
        "rebar.llm.contracts", fromlist=["epic_bug_screen_verdict_response_model"]
    ).epic_bug_screen_verdict_response_model()

    assert model(verdict="banana").verdict == "C"
    assert model(verdict=" a ").verdict == "A"
    assert model().verdict == "C"


# ------------------------------------------------------------------ forwarding block


def _tally_row(i: int, verdict: str = "A") -> dict:
    return {
        "ticket_id": f"bug{i:02d}-0000-0000-0000",
        "title": f"widget bug {i}",
        "verdict": verdict,
        "citation": f"defect in widget {i}",
    }


def test_candidate_block_caps_at_eight_with_overflow_line() -> None:
    tally = [_tally_row(i) for i in range(10)]

    block = ebs.candidate_block(tally, screen_overflow=3)

    for i in range(8):
        assert f"bug{i:02d}-0000-0000-0000" in block
        assert f"widget bug {i}" in block
        assert f"defect in widget {i}" in block
    assert "bug08" not in block
    assert "bug09" not in block
    # titles-only overflow: the 2 uncapped A-verdicts by title, plus the unevaluated count
    assert "widget bug 8" in block
    assert "widget bug 9" in block
    assert "3" in block  # unevaluated-overflow count surfaces


def test_candidate_block_empty_when_no_a_verdicts() -> None:
    tally = [_tally_row(0, "B"), _tally_row(1, "C")]

    assert ebs.candidate_block(tally, screen_overflow=0) == ""


def test_run_screen_degrades_open_on_total_failure(rebar_repo: Path) -> None:
    epic, _child = _epic_with_child(rebar_repo)
    rebar.create_ticket("bug", "fresh bug", repo_root=str(rebar_repo))

    def boom(bug: dict, sp: str) -> dict:
        raise RuntimeError("no model")

    out = ebs.run_screen(
        epic,
        {"title": "e", "description": "", "ticket_id": epic},
        str(rebar_repo),
        screen_fn=boom,
    )

    # Per-candidate failures degrade to C -> nothing surfaced, close proceeds.
    assert out["block"] == ""
    assert all(row["verdict"] == "C" for row in out["tally"])


# ------------------------------------------------------------------ precheck integration


def _precheck_ctx(ticket_id: str, repo: Path):
    from rebar.llm.workflow.executor import StepContext

    return StepContext(
        run_id="r1",
        step_id="s1",
        kind="uses",
        step={},
        inputs={"ticket_id": ticket_id},
        workflow={},
        target_ticket=ticket_id,
        repo_root=str(repo),
    )


def test_precheck_floor_short_circuits_without_llm(rebar_repo: Path) -> None:
    from rebar.llm.workflow.gate_ops import completion_precheck

    epic, child = _epic_with_child(rebar_repo)
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))
    bug = rebar.create_ticket("bug", "parser drops widgets", repo_root=str(rebar_repo))
    rebar.link(bug, child, "caused_by", repo_root=str(rebar_repo))

    out = completion_precheck(_precheck_ctx(epic, rebar_repo))

    assert out["run_verify"] is False
    assert out["precheck_failed"] is True
    assert out["verdict"]["verdict"] == "FAIL"
    assert out["verdict"]["runner"] == "deterministic"
    joined = " ".join(f["detail"] for f in out["verdict"]["findings"])
    assert bug in joined


def test_precheck_forwards_candidate_block_inside_fence(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm.workflow import gate_ops

    epic, child = _epic_with_child(rebar_repo)
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))

    sentinel = "UNRESOLVED BUG CANDIDATES\n- bug00-0000-0000-0000 widget bug 0"
    monkeypatch.setattr(
        ebs,
        "run_screen",
        lambda *a, **k: {"block": sentinel, "tally": [_tally_row(0)], "overflow": 0},
    )

    out = gate_ops.completion_precheck(_precheck_ctx(epic, rebar_repo))

    assert out["run_verify"] is True
    fenced = out["context"]
    assert sentinel in fenced
    # INSIDE the fence: the injection delimiter must still wrap the whole context.
    assert fenced.index(sentinel) < fenced.index("</untrusted_ticket_context>")
    assert fenced.index("<untrusted_ticket_context>") < fenced.index(sentinel)


def test_precheck_skips_screen_for_non_epics(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm.workflow import gate_ops

    task = rebar.create_ticket("task", "plain task", repo_root=str(rebar_repo))

    def _fail(*a, **k):  # pragma: no cover - the assertion is that this is never reached
        raise AssertionError("screen must not run for non-epics")

    monkeypatch.setattr(ebs, "run_screen", _fail)
    monkeypatch.setattr(completion, "epic_bug_floor_findings", _fail)

    out = gate_ops.completion_precheck(_precheck_ctx(task, rebar_repo))

    assert out["run_verify"] is True


# ------------------------------------------------------------------ sidecar tally


def test_screen_tally_lands_in_sidecar(rebar_repo: Path) -> None:
    epic, _child = _epic_with_child(rebar_repo)
    tally = [_tally_row(0), _tally_row(1, "C")]

    ok = sidecar.emit_screen_tally(epic, tally, overflow=4, repo_root=str(rebar_repo))

    assert ok is True
    rec = sidecar.latest_screen_tally(epic, repo_root=str(rebar_repo))
    assert rec is not None
    assert rec["schema"] == sidecar.SCHEMA_SCREEN
    assert rec["overflow"] == 4
    assert [row["verdict"] for row in rec["tally"]] == ["A", "C"]
    assert rec["tally"][0]["citation"] == "defect in widget 0"


def test_run_screen_records_tally_in_sidecar(rebar_repo: Path) -> None:
    epic, _child = _epic_with_child(rebar_repo)
    bug = rebar.create_ticket("bug", "fresh bug", repo_root=str(rebar_repo))

    out = ebs.run_screen(
        epic,
        {"title": "e", "description": "", "ticket_id": epic},
        str(rebar_repo),
        screen_fn=lambda b, sp: {"verdict": "A", "citation": "defect"},
    )

    assert out["block"] != ""
    rec = sidecar.latest_screen_tally(epic, repo_root=str(rebar_repo))
    assert rec is not None
    assert [row["ticket_id"] for row in rec["tally"]] == [bug]


# ------------------------------------------------------------------ regression shapes


def test_regression_30a2_shape_forwarded(rebar_repo: Path) -> None:
    """30a2-shape (must-block tier): created during the window, unlinked, screened A ->
    the candidate block carries title + citation + id for the verifier to adjudicate."""
    epic, _child = _epic_with_child(rebar_repo)
    bug = rebar.create_ticket(
        "bug", "token-bin scaling breaks under load", repo_root=str(rebar_repo)
    )

    out = ebs.run_screen(
        epic,
        {"title": "token reduction", "description": "", "ticket_id": epic},
        str(rebar_repo),
        screen_fn=lambda b, sp: {"verdict": "A", "citation": "bin scaling is epic deliverable"},
    )

    assert bug in out["block"]
    assert "token-bin scaling breaks under load" in out["block"]
    assert "bin scaling is epic deliverable" in out["block"]


def test_regression_c8ed_shape_is_candidate(rebar_repo: Path) -> None:
    """c8ed-shape (must-pass tier): a bug SUPERSEDING an investigation epic is linked, so it
    IS a candidate — the filter must not pre-judge it; disposition (its supersedes link) is
    the VERIFIER's store-grounded call, live-calibrated under the operator-attested AC."""
    epic, _child = _epic_with_child(rebar_repo)
    bug = rebar.create_ticket("bug", "root cause found: supersedes epic", repo_root=str(rebar_repo))
    rebar.link(bug, epic, "supersedes", repo_root=str(rebar_repo))

    assert bug in _candidate_ids(epic, rebar_repo)
