"""Deterministic G5 decomposition signal (task spangly-beggarly-blackrhino).

G5 (decomposition) once false-flagged an epic that already had 6 children as a "flat,
undecomposed list" because it judged from ticket TEXT and counted children itself. This
suite covers the two-part fix in ``det_floor.py`` + ``pass1.py``:

  * ``decomposition_state_block`` — an authoritative store-derived child summary INJECTED
    into the G5 finder context (AC1);
  * ``veto_undecomposed_g5`` — a deterministic post-Pass-1 BACKSTOP that drops a residual
    G5 decomposition-ABSENCE finding when the ticket has children, while PRESERVING a G5
    finding about child altitude/content (AC2).

Proving command:
    .venv/bin/pytest tests/unit/test_g5_decomp_det.py -v
"""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import det_floor, pass1, passes, registry
from rebar.llm.plan_review.det_floor import PlanContext


def _ctx(children: list[dict], *, ttype: str = "epic", repo_root: str | None = None) -> PlanContext:
    return PlanContext(
        ticket_id="epic-0000-0000-0001",
        ticket_type=ttype,
        title="Redesign the widget pipeline",
        description="## Acceptance Criteria\n- [ ] the pipeline is redesigned\n",
        children=children,
        repo_root=repo_root,
    )


def _children(n: int) -> list[dict]:
    return [
        {
            "ticket_id": f"c{i}",
            "alias": f"child-{i}",
            "title": f"Child work item {i}",
            "status": "open",
        }
        for i in range(n)
    ]


class _Capture:
    """A runner that records every RunRequest and returns no findings."""

    name = "capture"

    def __init__(self) -> None:
        self.reqs: list = []

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req):
        self.reqs.append(req)
        return {"findings": []}


# ── decomposition_state_block (AC1: authoritative store-sourced child summary) ───────────
def test_block_is_empty_without_children() -> None:
    assert det_floor.decomposition_state_block(_ctx([])) == ""


def test_block_names_children_as_ground_truth() -> None:
    block = det_floor.decomposition_state_block(_ctx(_children(6)))
    assert "DECOMPOSITION STATE (from store)" in block
    assert "6 direct child" in block
    # every child appears with alias/title/status
    assert "child-0" in block and "child-5" in block
    assert "Child work item 3" in block
    assert "(open)" in block
    # the finder is told NOT to flag the ticket as flat/undecomposed
    lowered = block.lower()
    assert "undecomposed" in lowered and "do not" in lowered


def test_block_child_status_falls_back_to_state_subdict() -> None:
    ctx = _ctx(
        [
            {
                "ticket_id": "c9",
                "alias": "c-nine",
                "title": "Nine",
                "state": {"status": "in_progress"},
            }
        ]
    )
    assert "(in_progress)" in det_floor.decomposition_state_block(ctx)


# ── veto_undecomposed_g5 (AC2) ───────────────────────────────────────────────────────────
_FLAT_G5 = {"finding": "This epic is a flat, undecomposed list of work items.", "criteria": ["G5"]}


def test_veto_drops_flat_finding_when_children_exist() -> None:
    kept, vetoed = det_floor.veto_undecomposed_g5([_FLAT_G5], _ctx(_children(6)))
    assert kept == []
    assert len(vetoed) == 1


def test_no_veto_for_genuinely_monolithic_childless_ticket() -> None:
    # AC3: a genuinely monolithic childless ticket still yields its G5 finding.
    kept, vetoed = det_floor.veto_undecomposed_g5([_FLAT_G5], _ctx([]))
    assert kept == [_FLAT_G5]
    assert vetoed == []


def test_g5_altitude_finding_preserved_even_with_children() -> None:
    # AC2: G5 still judges child content/altitude — an altitude finding is NOT a
    # decomposition-ABSENCE claim, so it survives even though children exist.
    altitude = {
        "finding": "The children sit at implementation-task altitude; an epic's direct "
        "children should be stories, not file-level tasks.",
        "criteria": ["G5"],
    }
    kept, vetoed = det_floor.veto_undecomposed_g5([altitude], _ctx(_children(6)))
    assert kept == [altitude]
    assert vetoed == []


def test_veto_only_targets_g5_findings() -> None:
    # A non-G5 finding whose prose happens to say "undecomposed" is never touched.
    other = {"finding": "flat, undecomposed prose", "criteria": ["T8"]}
    kept, vetoed = det_floor.veto_undecomposed_g5([other], _ctx(_children(6)))
    assert kept == [other]
    assert vetoed == []


def test_veto_regression_six_children_flat_list() -> None:
    # The exact historical misfire: an epic with 6 children flagged "flat, undecomposed
    # list". It must now be vetoed.
    finding = {"finding": "The epic is a flat, undecomposed list.", "criteria": ["G5"]}
    kept, vetoed = det_floor.veto_undecomposed_g5([finding], _ctx(_children(6)))
    assert vetoed and not kept


def test_veto_preserves_other_findings_alongside_a_vetoed_g5() -> None:
    keep_me = {"finding": "AC has no verify command", "criteria": ["E6"]}
    kept, vetoed = det_floor.veto_undecomposed_g5([_FLAT_G5, keep_me], _ctx(_children(3)))
    assert kept == [keep_me]
    assert vetoed == [_FLAT_G5]


# ── AC1 wiring: the block reaches the finder instructions (and only when asked) ──────────
def test_pass1_chunk_injects_extra_context_into_instructions() -> None:
    cap = _Capture()
    block = det_floor.decomposition_state_block(_ctx(_children(4)))
    passes.pass1_chunk(
        cap,
        LLMConfig(runner="fake"),
        plan="p",
        chunk=[{"id": "G5", "name": "decomposition"}],
        extra_context=block,
    )
    assert "DECOMPOSITION STATE (from store)" in cap.reqs[0].instructions


def test_pass1_chunk_has_no_context_by_default() -> None:
    cap = _Capture()
    passes.pass1_chunk(cap, LLMConfig(runner="fake"), plan="p", chunk=[{"id": "E2", "name": "x"}])
    assert "DECOMPOSITION STATE" not in cap.reqs[0].instructions


# ── AC1 end-to-end: run_pass1 injects the block iff the ticket has children ──────────────
def _run_pass1_capture(ctx: PlanContext) -> _Capture:
    cap = _Capture()
    pass1.run_pass1(ctx, LLMConfig(runner="fake"), cap, [registry.by_id()["G5"]], [], {})
    return cap


def test_run_pass1_injects_block_when_children_exist(tmp_path) -> None:
    cap = _run_pass1_capture(_ctx(_children(6), repo_root=str(tmp_path)))
    assert any("DECOMPOSITION STATE (from store)" in r.instructions for r in cap.reqs)


def test_run_pass1_omits_block_for_childless_ticket(tmp_path) -> None:
    cap = _run_pass1_capture(_ctx([], repo_root=str(tmp_path)))
    assert cap.reqs  # the G5 finder still ran
    assert not any("DECOMPOSITION STATE" in r.instructions for r in cap.reqs)
