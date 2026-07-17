"""Pass-4 coaching tests for the project-owned `project-portability` move
(epic jira-reb-1003, task truthful-specified-silverfish).

Exercise the REAL committed `.rebar/plan_review_moves.json` at the repo root through the
normal coaching path: the project move registry load, applicability against representative
trigger sets, deterministic template rendering, and non-interference with the packaged
built-in move registry.
"""

from __future__ import annotations

from pathlib import Path

from rebar.llm.plan_review import orchestrator
from rebar.llm.review_kernel.coach import applicable_moves, render_coach_notes

REPO_ROOT = str(Path(__file__).resolve().parents[2])
MOVE_ID = "project-portability"
EXPECTED_NAME = "restore rebar portability"
EXPECTED_TEMPLATE = (
    "Rework {subject} so it remains portable across supported rebar client shapes; "
    "keep project-specific behavior in project configuration or an explicit extension boundary."
)
EXPECTED_SENTENCE = EXPECTED_TEMPLATE.format(subject="the Gerrit landing workflow")


def _reg() -> dict:
    return orchestrator.load_move_registry(repo_root=REPO_ROOT)


# ── the move loads with the exact authored fields ────────────────────────────────
def test_move_id_loads():
    assert MOVE_ID in _reg()


def test_move_name():
    assert _reg()[MOVE_ID]["name"] == EXPECTED_NAME


def test_move_template():
    assert _reg()[MOVE_ID]["template"] == EXPECTED_TEMPLATE


def test_move_trigger():
    assert _reg()[MOVE_ID]["applies_when"] == ["project.portability"]


# ── applicability against representative trigger sets ────────────────────────────
def test_applies_to_portability():
    assert MOVE_ID in applicable_moves(_reg(), {"project.portability"})


def test_ignores_empty_triggers():
    reg = _reg()
    assert MOVE_ID in reg  # the move exists ...
    assert MOVE_ID not in applicable_moves(reg, set())  # ... but is excluded by trigger mismatch


def test_ignores_unrelated_findings():
    reg = _reg()
    assert MOVE_ID in reg  # the move exists ...
    assert MOVE_ID not in applicable_moves(reg, {"F1"})  # ... but not for an unrelated trigger


# ── deterministic render through the normal coaching path ────────────────────────
def test_deterministic_render():
    applicable = applicable_moves(_reg(), {"project.portability"})
    notes = render_coach_notes(
        [{"move_id": MOVE_ID, "subject": "the Gerrit landing workflow", "finding_refs": ["f1"]}],
        applicable,
    )
    assert len(notes) == 1
    assert notes[0]["coaching"] == EXPECTED_SENTENCE
    assert notes[0]["move_name"] == EXPECTED_NAME


# ── the packaged built-in registry is left untouched ─────────────────────────────
def test_project_owned_only():
    # Loading the project overlay must not mutate the packaged MOVE_REGISTRY mapping:
    # the project move is merged into a fresh copy, never into the built-in dict.
    packaged_ids = set(orchestrator.MOVE_REGISTRY)
    reg = _reg()
    assert MOVE_ID in reg  # present in the merged view
    assert MOVE_ID not in orchestrator.MOVE_REGISTRY  # ...but never in the packaged map
    assert set(orchestrator.MOVE_REGISTRY) == packaged_ids  # packaged keys unchanged
    # every built-in id survives the merge
    assert packaged_ids <= set(reg)
